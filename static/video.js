"use strict";
const cfg=document.getElementById('callConfig'),status=document.getElementById('callStatus');
let pc=null,stream=null,after=0,active=false,pending=[],notified=false;
const join=document.getElementById('joinCall');
async function send(kind,payload){const r=await fetch(cfg.dataset.signals,{method:'POST',credentials:'same-origin',body:new URLSearchParams({csrf_token:cfg.dataset.csrf,kind,payload:JSON.stringify(payload)})});if(!r.ok)throw new Error('Сигнал дзвінка відхилено. Перевірте вхід або створіть новий дзвінок.');}
function stop(){active=false;if(pc){pc.close();pc=null;}stream?.getTracks().forEach(t=>t.stop());document.getElementById('localVideo').srcObject=null;document.getElementById('remoteVideo').srcObject=null;join.disabled=true;document.getElementById('muteCall').disabled=true;document.getElementById('cameraCall').disabled=true;}
async function poll(){
 if(!active)return;
 try{
  const r=await fetch(cfg.dataset.signals+'?after='+after,{cache:'no-store',credentials:'same-origin'});if(!r.ok)throw new Error('Дзвінок недоступний або його час минув.');
  const data=await r.json();if(!active)return;
  if(data.ended){stop();status.textContent='Дзвінок завершено.';return;}
  for(const s of data.signals){
   if(!active)return;
   if(s.kind==='offer'){await pc.setRemoteDescription(s.payload);for(const c of pending)await pc.addIceCandidate(c);pending=[];await pc.setLocalDescription(await pc.createAnswer());await send('answer',pc.localDescription.toJSON());}
   else if(s.kind==='answer'){await pc.setRemoteDescription(s.payload);for(const c of pending)await pc.addIceCandidate(c);pending=[];}
   else if(s.kind==='candidate'){if(pc.remoteDescription)await pc.addIceCandidate(s.payload);else pending.push(s.payload);}
   else if(s.kind==='hangup'){stop();status.textContent='Інший учасник завершив дзвінок.';return;}
   after=s.id;
  }
 }catch(e){stop();status.textContent=e.message;return;}
 if(active)setTimeout(poll,1500);
}
join.addEventListener('click',async()=>{
 if(!window.isSecureContext||!navigator.mediaDevices?.getUserMedia){status.textContent='Камера потребує довіреного HTTPS. Відкрийте локальну HTTPS-адресу після встановлення сертифіката за DEPLOY.md.';return;}
 join.disabled=true;
 try{
  stream=await navigator.mediaDevices.getUserMedia({video:{facingMode:'user'},audio:true});document.getElementById('localVideo').srcObject=stream;
  pc=new RTCPeerConnection({iceServers:[]});active=true;
  stream.getTracks().forEach(t=>pc.addTrack(t,stream));
  pc.ontrack=e=>{document.getElementById('remoteVideo').srcObject=e.streams[0];};
  pc.onicecandidate=e=>{if(e.candidate&&active)send('candidate',e.candidate.toJSON()).catch(err=>{stop();status.textContent=err.message;});};
  pc.onconnectionstatechange=()=>{if(!pc)return;status.textContent=pc.connectionState==='connected'?'З’єднання встановлено.':pc.connectionState==='failed'?'З’єднання не встановлено. Перевірте Wi-Fi, HTTPS і створіть новий дзвінок.':'Очікуємо іншого учасника…';};
  document.getElementById('muteCall').disabled=false;document.getElementById('cameraCall').disabled=false;
  if(cfg.dataset.initiator==='true'){await pc.setLocalDescription(await pc.createOffer());await send('offer',pc.localDescription.toJSON());}
  status.textContent='Очікуємо іншого учасника…';poll();
 }catch(e){stop();status.textContent='Не вдалося приєднатися: '+e.message+'. Дозвольте камеру й мікрофон у налаштуваннях браузера.';}
});
for(const [id,kind,label] of [['muteCall','audio','мікрофон'],['cameraCall','video','камеру']])document.getElementById(id).addEventListener('click',()=>{const tracks=stream?.getTracks().filter(t=>t.kind===kind)||[];for(const t of tracks)t.enabled=!t.enabled;document.getElementById(id).textContent=(tracks[0]?.enabled?'Вимкнути ':'Увімкнути ')+label;});
document.getElementById('endCall').addEventListener('click',async()=>{try{await send('hangup',{});notified=true;}catch(e){status.textContent=e.message;}finally{stop();if(notified)status.textContent='Дзвінок завершено.';}});
window.addEventListener('pagehide',()=>{if(active&&!notified){navigator.sendBeacon(cfg.dataset.signals,new URLSearchParams({csrf_token:cfg.dataset.csrf,kind:'hangup',payload:'{}'}));}stop();});
