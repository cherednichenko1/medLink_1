"use strict";
const mini=document.getElementById('miniChat'),toggle=document.getElementById('miniChatToggle'),select=document.getElementById('miniChatContact'),list=document.getElementById('miniChatMessages'),status=document.getElementById('miniChatStatus');
let threadId=null,after=0,generation=0,timer=null,controller=null;
const polls=new Set();
const report=message=>{status.textContent=message;window.medlinkNotify?.(message,'error');};
async function api(url,body,signal){const r=await fetch(url,{method:body?'POST':'GET',credentials:'same-origin',cache:'no-store',headers:{Accept:'application/json'},body:body?new URLSearchParams({csrf_token:mini.dataset.csrf,...body}):undefined,signal});if(!r.ok)throw new Error(r.status===429?'Зачекайте хвилину перед новим повідомленням.':'Чат недоступний. Перевірте вхід і доступ до співрозмовника.');return r.json();}
function cancel(){++generation;clearTimeout(timer);controller?.abort();}
function close(){cancel();mini.hidden=true;toggle.setAttribute('aria-expanded','false');toggle.focus();}
document.getElementById('miniChatClose').addEventListener('click',close);
mini.addEventListener('keydown',e=>{if(e.key==='Escape'){e.preventDefault();close();}});
async function poll(current){
 if(mini.hidden||!threadId||current!==generation||polls.has(current))return;
 polls.add(current);
 try{
  const data=await api('/messages/'+threadId+'/updates?after='+after,null,controller.signal);
  if(current!==generation||mini.hidden)return;
  const nearBottom=list.scrollHeight-list.scrollTop-list.clientHeight<80;
  for(const m of data.messages){
   if(m.id<=after)continue;
   const box=document.createElement('article');box.className='message '+(m.role===mini.dataset.role?'mine':'');
   const label=document.createElement('small');label.textContent=(m.role==='doctor'?'Лікар':'Пацієнт')+' · '+new Date(m.date).toLocaleTimeString('uk-UA',{hour:'2-digit',minute:'2-digit'});box.append(label);
   const text=document.createElement('p');text.textContent=m.text;box.append(text);
   if(m.attachment){const a=document.createElement('a');a.href='/documents/'+m.attachment+'/download';a.textContent=m.title||'Документ';box.append(a);}
   list.append(box);after=m.id;
  }
  if(nearBottom)list.scrollTop=list.scrollHeight;status.textContent='';
 }catch(e){if(e.name!=='AbortError'&&current===generation)report(e.message);}
 finally{polls.delete(current);}
 if(!mini.hidden&&current===generation)timer=setTimeout(()=>poll(current),2500);
}
async function choose(){
 cancel();const current=generation;controller=new AbortController();threadId=null;after=0;list.replaceChildren();document.getElementById('miniChatFull').href='/messages';document.getElementById('miniChatFiles').hidden=true;document.getElementById('miniChatCall').disabled=true;
 const partner=select.value;if(!partner){status.textContent='Оберіть співрозмовника.';return;}
 try{
  const data=await api('/messages/start/'+partner,{},controller.signal);if(current!==generation)return;
  threadId=data.thread_id;document.getElementById('miniChatFull').href='/messages/'+threadId;
  const files=document.getElementById('miniChatFiles');files.href='/patients/'+select.selectedOptions[0].dataset.patient+'/documents';files.hidden=false;
  document.getElementById('miniChatCall').disabled=false;await poll(current);
 }catch(e){if(e.name!=='AbortError'&&current===generation)report(e.message);}
}
select.addEventListener('change',choose);
toggle.addEventListener('click',async()=>{
 if(!mini.hidden){close();return;}
 mini.hidden=false;threadId=null;after=0;list.replaceChildren();toggle.setAttribute('aria-expanded','true');cancel();const current=generation;controller=new AbortController();
 try{
  const data=await api('/messages/contacts',null,controller.signal);if(current!==generation)return;
  const previous=select.value;select.replaceChildren(new Option('Оберіть контакт',''));
  for(const contact of data.contacts){const option=new Option(contact.name,contact.id);option.dataset.patient=contact.patient_id;select.append(option);}
  if([...select.options].some(o=>o.value===previous))select.value=previous;
  status.textContent=data.contacts.length?'Оберіть співрозмовника.':'Спочатку потрібен запис до лікаря або пацієнт у вашому кабінеті.';select.focus();if(select.value)choose();
 }catch(e){if(e.name!=='AbortError')report(e.message);}
});
document.getElementById('miniChatForm').addEventListener('submit',async e=>{
 e.preventDefault();if(!threadId){report('Спочатку оберіть співрозмовника.');return;}
 const input=document.getElementById('miniChatText'),text=input.value.trim();if(!text)return;
 const sendingThread=threadId,current=generation,button=e.target.querySelector('button');button.disabled=true;
 try{await api('/messages/'+sendingThread,{text});if(current===generation){input.value='';clearTimeout(timer);await poll(current);}}
 catch(error){report(error.message);}finally{button.disabled=false;}
});
document.getElementById('miniChatCall').addEventListener('click',async()=>{if(!threadId)return;try{const data=await api('/messages/'+threadId+'/call',{});window.location.assign(data.url);}catch(e){report(e.message);}});
window.addEventListener('pagehide',cancel);
