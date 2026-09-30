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
