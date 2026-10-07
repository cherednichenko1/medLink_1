'use strict';
const registerRole = document.getElementById('registerRole');
const doctorFields = document.getElementById('doctorFields');
function updateRegistration() {
  if (!doctorFields || !registerRole) return;
  const doctor = registerRole.value === 'doctor';
  doctorFields.hidden = !doctor;
  doctorFields.disabled = !doctor;
}
registerRole?.addEventListener('change', updateRegistration);
updateRegistration();
const loginRole = document.getElementById('roleSelect');
function updateLoginLabel() {
  const label = document.getElementById('loginLabel');
  if (label && loginRole) label.textContent = loginRole.value === 'doctor' ? 'Email або РНОКПП:' : 'Email:';
}
loginRole?.addEventListener('change', updateLoginLabel);
updateLoginLabel();
document.querySelectorAll('[data-submit-on-change]').forEach(select => {
  select.addEventListener('change', () => select.form.requestSubmit());
});
document.querySelectorAll('[data-dismiss-notice]').forEach(button => {
  button.addEventListener('click', () => button.closest('.notice').remove());
});
const menuButton = document.querySelector('.menu-toggle');
menuButton?.addEventListener('click', () => {
  const open = menuButton.getAttribute('aria-expanded') !== 'true';
  menuButton.setAttribute('aria-expanded', String(open));
  document.getElementById('siteNav').classList.toggle('open', open);
});

const qrDialog = document.getElementById('qrDialog');
const qrImage = document.getElementById('qrImage');
const qrLoading = document.getElementById('qrLoading');
const qrError = document.getElementById('qrError');
const shareConsent = document.getElementById('qrShareConsent');
let activeQrButton = null;
let qrGeneration = 0;
let shareController = null;
function resetQrImage(source) {
  qrError.hidden = true;
  qrImage.hidden = true;
  qrLoading.hidden = false;
  qrImage.src = source;
}
qrImage?.addEventListener('load', () => { qrLoading.hidden = true; qrImage.hidden = false; });
qrImage?.addEventListener('error', () => {
  qrLoading.hidden = true;
  qrImage.hidden = true;
  qrError.textContent = 'Не вдалося відкрити QR. Оновіть сторінку або увійдіть знову.';
  qrError.hidden = false;
});
document.querySelectorAll('[data-qr-src]').forEach(button => {
  button.addEventListener('click', () => {
    shareController?.abort();
    qrGeneration += 1;
    activeQrButton = button;
    document.getElementById('qrTitle').textContent = button.dataset.qrTitle || 'QR-код';
    const privateQr = Boolean(button.dataset.qrScope);
    document.getElementById('qrShareSection').hidden = !privateQr;
    document.getElementById('qrAccessHint').textContent = privateQr ? 'На іншому телефоні потрібно увійти. Або дозвольте тимчасовий перегляд нижче.' : (button.dataset.qrSrc === '/workspace/qr' || button.dataset.qrSrc.startsWith('/messages/')) ? 'Відскануйте QR на іншому пристрої й увійдіть у свій кабінет. QR не містить пароля.' : 'Публічна сторінка лікаря відкривається без входу.';
    document.getElementById('qrNetworkHint').hidden = !['localhost', '127.0.0.1', 'medlink.local', '::1'].includes(qrDialog.dataset.baseHost);
    shareConsent.checked = false;
    shareConsent.disabled = false;
    resetQrImage(button.dataset.qrSrc);
    qrDialog.showModal();
  });
});
shareConsent?.addEventListener('change', async () => {
  const generation = ++qrGeneration;
  shareController?.abort();
  if (!shareConsent.checked) {
    resetQrImage(activeQrButton.dataset.qrSrc);
    document.getElementById('qrAccessHint').textContent = 'Цей QR потребує входу. Попереднє тимчасове посилання, якщо видане, діє до завершення його 15 хвилин.';
    return;
  }
  shareController = new AbortController();
  qrImage.hidden = true;
  qrLoading.hidden = false;
  qrError.hidden = true;
  try {
    const response = await fetch('/qr/share', {
      method: 'POST', credentials: 'same-origin', signal: shareController.signal,
      body: new URLSearchParams({csrf_token: document.getElementById('qrCsrf').value,
        scope: activeQrButton.dataset.qrScope, patient_id: activeQrButton.dataset.patientId})
    });
    if (!response.ok) throw new Error('Не вдалося надати доступ. Оновіть сторінку та перевірте вхід.');
    const data = await response.json();
    if (generation !== qrGeneration || !qrDialog.open) return;
    resetQrImage(data.image);
    document.getElementById('qrAccessHint').textContent = 'Цей QR відкриває вибрану сторінку без входу протягом 15 хвилин. Не публікуйте його.';
  } catch (error) {
    if (error.name === 'AbortError' || generation !== qrGeneration) return;
    qrLoading.hidden = true;
    qrError.textContent = error.message;
    qrError.hidden = false;
    shareConsent.checked = false;
  }
});
document.querySelectorAll('[data-close-qr]').forEach(button => button.addEventListener('click', () => qrDialog.close()));
qrDialog?.addEventListener('click', event => {
  if (event.target !== qrDialog) return;
  const box = qrDialog.getBoundingClientRect();
  if (event.clientX < box.left || event.clientX > box.right || event.clientY < box.top || event.clientY > box.bottom) qrDialog.close();
});
qrDialog?.addEventListener('close', () => {
  ++qrGeneration;
  shareController?.abort();
  qrImage.removeAttribute('src');
  qrImage.hidden = true;
  activeQrButton?.focus();
});
if (window.location.pathname === '/shared' && window.location.search) history.replaceState(null, '', window.location.pathname);
const redeemForm = document.getElementById('sharedRedeem');
if (redeemForm) {
  const token = window.location.hash.slice(1);
  history.replaceState(null, '', window.location.pathname);
  if (token && token.length < 4096) {
    document.getElementById('sharedToken').value = token;
    redeemForm.requestSubmit();
  } else {
    document.getElementById('sharedStatus').textContent = 'Посилання не містить дійсного QR-коду. Попросіть новий код.';
    redeemForm.hidden = true;
  }
}
