"use strict";
const list=document.getElementById('messageList'), status=document.getElementById('messageStatus');
let after=0, stopped=false;
async function pollMessages(){
 if(stopped) return;
 try{
  const response=await fetch(list.dataset.updates+'?after='+after,{credentials:'same-origin',cache:'no-store'});
  if(!response.ok) throw new Error('Переписка недоступна. Оновіть сторінку й перевірте вхід.');
  const data=await response.json();
  for(const m of data.messages){
   const box=document.createElement('article');box.className='message '+(m.role===list.dataset.role?'mine':'');
   const label=document.createElement('small');label.textContent=(m.role==='doctor'?'Лікар':'Пацієнт')+' · '+new Date(m.date).toLocaleString('uk-UA');box.append(label);
   const text=document.createElement('p');text.textContent=m.text;box.append(text);
   if(m.attachment){const a=document.createElement('a');a.href='/documents/'+m.attachment+'/download';a.textContent=m.title||'Документ';box.append(a);}
   list.append(box);after=m.id;
  }
  const call=document.getElementById('activeCall');call.hidden=!data.call;if(data.call)call.href='/calls/'+data.call;
  status.textContent='';
 }catch(e){status.textContent=e.message;}
 if(!stopped)setTimeout(pollMessages,2500);
}
window.addEventListener('pagehide',()=>{stopped=true;});pollMessages();
