"use strict";
const toastStack=document.getElementById('toastStack');
function scheduleToast(toast){
 if(toast.classList.contains('error'))return;
 let timer;const start=()=>{clearTimeout(timer);timer=setTimeout(()=>toast.remove(),7000);};
 toast.addEventListener('mouseenter',()=>clearTimeout(timer));toast.addEventListener('mouseleave',start);
 toast.addEventListener('focusin',()=>clearTimeout(timer));toast.addEventListener('focusout',start);start();
}
document.querySelectorAll('[data-toast]').forEach(scheduleToast);
const lastNotification=new Map();
window.medlinkNotify=(message,category='success')=>{
 if(!toastStack||!message)return;
 const key=category+message, now=Date.now();if(now-(lastNotification.get(key)||0)<10000)return;lastNotification.set(key,now);
 const toast=document.createElement('div');toast.className='notice toast '+(category==='error'?'error':'success');toast.setAttribute('role',category==='error'?'alert':'status');
 const text=document.createElement('span');text.textContent=message;toast.append(text);
 const close=document.createElement('button');close.type='button';close.textContent='×';close.setAttribute('aria-label','Закрити повідомлення');close.addEventListener('click',()=>toast.remove());toast.append(close);
 toastStack.append(toast);scheduleToast(toast);
};
