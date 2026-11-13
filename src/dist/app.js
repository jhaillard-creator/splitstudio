'use strict';
const $ = id => document.getElementById(id);
const modal = $('modal');
const jobs = new Map();
let local = false, activeJob = null, selected = null, modalVersion = 0;
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
const labels = {PUBLIC_TO_EVERYONE:'Tout le monde', MUTUAL_FOLLOW_FRIENDS:'Amis', FOLLOWER_OF_CREATOR:'Abonnés', SELF_ONLY:'Moi uniquement'};
async function api(path, body) {
  const response = await fetch(path, body === undefined ? {cache:'no-store'} : {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  let data;
  try { data = await response.json(); } catch {throw Error('Le studio local est inaccessible.');}
  if (!response.ok) {const error=Error(data.error || data.message || 'Impossible de terminer cette opération.');error.httpStatus=response.status;throw error;}
  return data;
}
function node(tag, content, className) {
  const el = document.createElement(tag);
  if (content !== undefined) el.textContent=content;
  if (className) el.className=className;
  return el;
}
function status(message, error=false) {
  $('status').hidden=!message; $('status').textContent=message;
  $('status').classList.toggle('error',error);
}
function show(markup) {
  modalVersion++;
  $('modalContent').innerHTML=markup;
  if (!modal.open) modal.showModal();
  return modalVersion;
}
modal.querySelector('.x').onclick=()=>modal.close();
modal.addEventListener('click', e=>{if(e.target===modal){const b=modal.getBoundingClientRect(); if(e.clientX<b.left||e.clientX>b.right||e.clientY<b.top||e.clientY>b.bottom)modal.close();}});
function launchLocal() {
  show('<h2>Ouvrir le studio sur Mac</h2><p class="modalcopy">Le montage et la transcription se font sur votre Mac. Téléchargez l’application, décompressez le ZIP et ouvrez <strong>Split Studio.app</strong>.</p><div class="actions"><a class="primary" href="/SplitStudio.zip" download>Télécharger Split Studio</a><a class="secondary" href="http://127.0.0.1:8765/" target="_blank" rel="noopener">Ouvrir le studio local</a></div>');
}
function demo() {
  selected=null;
  const video=$('previewVideo');video.pause();video.hidden=true;video.removeAttribute('src');video.load();
  document.querySelector('.exam').hidden=false;
  const dual=$('layout').value==='double';
  $('frame').classList.toggle('dual',dual);$('secondSample').hidden=!dual;
  $('previewLabel').textContent='Exemple de cadrage';
  $('durationTag').textContent=({'10':'10 s','180':'3 min','600':'10 min'})[document.querySelector('[name=seconds]:checked').value];
}
$('layout').onchange=()=>{
  const dual=$('layout').value==='double';$('secondWrap').hidden=!dual;$('second').required=dual;demo();
};
document.querySelectorAll('[name=seconds]').forEach(x=>x.onchange=demo);
document.querySelectorAll('.navitem').forEach(a=>a.onclick=()=>{
  document.querySelectorAll('.navitem').forEach(item=>item.classList.toggle('active',item===a));
});
function preview(clip) {
  selected=clip;document.querySelector('.exam').hidden=true;
  const video=$('previewVideo');video.hidden=false;video.src=clip.url;video.load();
  $('previewLabel').textContent=clip.name;$('durationTag').textContent='MP4';
  document.querySelector('.preview').scrollIntoView({block:'nearest'});
}
const downloads = new Map();
const stageNames = {
  download:'Téléchargement des sources', transcribe:'Transcription', analyze:'Analyse des meilleurs moments',
  render:'Montage des clips', done:'Génération terminée', queued:'En attente'
};
function meter(bar, pctLabel, amount) {
  if (typeof amount === 'number' && Number.isFinite(amount)) {
    const value=Math.min(100,Math.max(0,Math.floor(amount)));
    bar.value=value; pctLabel.textContent=value+' %';
  } else {
    bar.removeAttribute('value'); pctLabel.textContent='En cours…';
  }
}
function showGeneration(job) {
  $('generation').hidden=false;
  $('generationStage').textContent=job.status==='error'?'Génération interrompue':stageNames[job.stage]||'Génération';
  meter($('generationBar'),$('generationPct'),job.status==='done'?100:job.progress);
  if (job.status==='error' && !Number.isFinite(job.progress)) {
    $('generationPct').textContent='Échec'; $('generationBar').value=0;
  }
  status(job.message || 'Génération en cours…',job.status==='error');
}
function timestamp(seconds) {
  const total=Math.max(0,Math.floor(seconds));
  return Math.floor(total/60)+':'+String(total%60).padStart(2,'0');
}
function downloadMeter(clip) {
  const panel=node('div',undefined,'fileProgress'), head=node('div',undefined,'meterHead');
  const label=node('span','Téléchargement'), pct=node('span');
  const bar=node('progress'), note=node('small');
  note.setAttribute('role','status');bar.max=100;bar.setAttribute('aria-label','Téléchargement de '+clip.name);
  head.append(label,pct);panel.append(head,bar,note);panel.hidden=true;
  downloads.set(clip.url,state=>{panel.hidden=false;meter(bar,pct,state.percent);note.textContent=state.note||'';
    if(state.saved&&native()){const show=node('button','Afficher dans le Finder','reveal');show.type='button';show.onclick=()=>native().reveal(state.saved);note.append(' ',show);}
    if(state.native||state.error||state.cancelled){bar.hidden=true;pct.hidden=true;}else{bar.hidden=false;pct.hidden=false;}
  });
  return panel;
}
function saveLink(url,fileName) {
  const a=node('a');a.href=url;a.download=fileName;document.body.append(a);a.click();a.remove();
}
const transfers=new Map();
function setTransfer(url,patch) {
  const state={...transfers.get(url),...patch};transfers.set(url,state);
  downloads.get(url)?.(state);
}
// In the Mac app, files are saved through the native Save dialog.
const native=()=>window.pywebview?.api?.save_file?window.pywebview.api:null;
async function nativeSave(file,update){
  const bridge=native(), mb=n=>(n/1048576).toFixed(n<10485760?1:0)+' Mo';
  update({busy:true,percent:null,note:'Choisis où enregistrer…',native:false,error:false,cancelled:false});
  try{
    const started=await bridge.save_file(file.url,file.fileName);
    if(started.cancelled){update({cancelled:true,note:'Enregistrement annulé.'});return;}
    if(started.error)throw Error(started.error);
    const began=performance.now();
    update({note:file.fileName.endsWith('.zip')?'Préparation de l’archive…':'Enregistrement…'});
    for(;;){
      await sleep(150);
      const t=await bridge.transfer(started.token);
      if(t.error)throw Error(t.error);
      if(t.done){update({percent:100,note:'Enregistré : '+started.name+' · '+mb(t.received),saved:started.path});return;}
      if(t.received){
        const speed=t.received/Math.max(.001,(performance.now()-began)/1000);
        update({percent:t.total?Math.min(99,100*t.received/t.total):null,note:mb(t.received)+(t.total?' sur '+mb(t.total):'')+' · '+mb(speed)+'/s'});
      }
    }
  }catch(e){update({error:true,note:e.message||'Échec de l’enregistrement.'});}
  finally{update({busy:false});}
}
async function saveFile(file) {
  const key=file.url;
  if(transfers.get(key)?.busy)return;
  if(native())return nativeSave(file,patch=>setTransfer(key,patch));
  const controller=new AbortController();
  let writer;
  const update=patch=>setTransfer(key,patch);
  const zip=file.fileName.endsWith('.zip');
  // Beyond this size the browser's own download manager is safer than buffering in memory.
  const sizeLimit=1024*1024*1024;
  update({busy:true,percent:null,note:'Préparation du téléchargement…',native:false,error:false,cancelled:false});
  try {
    if (typeof window.showSaveFilePicker==='function') {
      const types=zip?[{description:'Archive ZIP',accept:{'application/zip':['.zip']}}]:[{description:'Clip MP4',accept:{'video/mp4':['.mp4']}}];
      const handle=await window.showSaveFilePicker({suggestedName:file.fileName,types});
      writer=await handle.createWritable();
    }
    update({note:zip?'Préparation de l’archive…':'Connexion…'});
    const response=await fetch(key,{signal:controller.signal,cache:'no-store'});
    if(!response.ok){let message='Le fichier ne peut pas être téléchargé.';try{message=(await response.json()).error||message;}catch{}throw Error(message);}
    const total=Number(response.headers.get('Content-Length'))||0;
    if(!response.body?.getReader || (!writer&&total>sizeLimit)){
      controller.abort();if(writer){await writer.abort();writer=null;}
      saveLink(key,file.fileName);update({native:true,note:'Fichier volumineux : suivez le transfert dans les téléchargements du navigateur.'});return;
    }
    const reader=response.body.getReader(), chunks=[], began=performance.now();
    let received=0, lastShown=0;
    const mb=n=>(n/1048576).toFixed(n<10485760?1:0)+' Mo';
    for(;;){
      const {done,value}=await reader.read();if(done)break;
      if(writer)await writer.write(value);else chunks.push(value);
      received+=value.byteLength;
      const now=performance.now();
      if(now-lastShown>120){
        lastShown=now;
        const speed=received/Math.max(.001,(now-began)/1000);
        const left=total&&speed?Math.ceil((total-received)/speed):null;
        update({percent:total?Math.min(99,100*received/total):null,
          note:mb(received)+(total?' sur '+mb(total):'')+' · '+mb(speed)+'/s'+(left!==null&&left>1?' · encore '+left+' s':'')});
      }
    }
    if(total&&received!==total)throw Error('Téléchargement incomplet. Relancez le transfert.');
    if(writer){await writer.close();writer=null;}else{
      const blobURL=URL.createObjectURL(new Blob(chunks,{type:zip?'application/zip':'video/mp4'}));
      saveLink(blobURL,file.fileName);setTimeout(()=>URL.revokeObjectURL(blobURL),60000);
    }
    update({percent:100,note:'Téléchargement terminé · '+mb(received)});
  }catch(e){
    controller.abort();if(writer)await writer.abort().catch(()=>{});
    if(e.name==='AbortError')update({cancelled:true,note:'Téléchargement annulé.'});
    else update({error:true,note:e.message||'Échec du téléchargement.'});
  }finally{update({busy:false});}
}
const saveClip=clip=>saveFile({url:clip.url,fileName:clip.name?clip.name+'.mp4':clip.id});
$('openLibrary').onclick=()=>openFolder('');

function jobLabel(job) {
  const when=new Date((job.created||0)*1000).toLocaleString('fr-FR',{day:'numeric',month:'short',hour:'2-digit',minute:'2-digit'});
  const kind=job.highlights?'Moments forts IA':'Vidéo entière';
  const size=({10:'10 s',180:'3 min',600:'10 min'})[job.seconds]||'';
  return [when,kind,size].filter(Boolean).join(' · ');
}
// ---- Folders & selection -------------------------------------------------
const picked=new Set(), collapsed=new Set();
const refOf=(job,clip)=>job.id+':'+clip.id;
function folderOf(job,clip){
  if(clip.folder)return clip.folder;
  if(job.folder)return job.folder;
  const d=new Date((job.created||0)*1000);
  return 'Vidéo du '+d.toLocaleDateString('fr-FR')+' '+String(d.getHours()).padStart(2,'0')+'h'+String(d.getMinutes()).padStart(2,'0');
}
function folders(){
  const map=new Map();
  for(const job of jobs.values())for(const clip of job.clips||[]){
    const name=folderOf(job,clip);
    if(!map.has(name))map.set(name,{name,items:[],latest:0,jobs:new Set()});
    const f=map.get(name);f.items.push({job,clip});f.jobs.add(job.id);f.latest=Math.max(f.latest,job.created||0);
  }
  for(const f of map.values())f.items.sort((a,b)=>(b.job.created||0)-(a.job.created||0)||(a.clip.start||0)-(b.clip.start||0));
  return Array.from(map.values()).sort((a,b)=>b.latest-a.latest);
}
function archiveURL(items,name){
  const q=new URLSearchParams();for(const {job,clip} of items)q.append('c',refOf(job,clip));q.set('name',name);
  return '/api/archive?'+q;
}
function selectedItems(){
  const out=[];for(const job of jobs.values())for(const clip of job.clips||[])if(picked.has(refOf(job,clip)))out.push({job,clip});return out;
}
async function reloadJobs(){
  const history=await api('/api/jobs');
  for(const job of history.jobs){const live=jobs.get(job.id);if(!(live&&live.status==='running'&&activeJob===job.id))jobs.set(job.id,job);}
  renderLibrary();
}
function renderSelection(){
  const bar=$('selectionBar'), items=selectedItems();
  for(const key of Array.from(picked))if(!items.some(({job,clip})=>refOf(job,clip)===key))picked.delete(key);
  bar.hidden=!items.length;if(!items.length)return;
  bar.replaceChildren();
  const label=node('b',items.length+' clip'+(items.length>1?'s':'')+' sélectionné'+(items.length>1?'s':''));
  const move=node('button','Ranger dans…','all');move.onclick=()=>moveDialog(items);
  const ready=items.filter(({job})=>job.status==='done');
  const get=node('button','Télécharger la sélection');get.disabled=!ready.length;
  const bundle={url:archiveURL(ready,'Sélection Split Studio'),name:'la sélection'};
  get.onclick=()=>saveFile({url:bundle.url,fileName:'Sélection Split Studio.zip'});
  const clear=node('button','Désélectionner');clear.onclick=()=>{picked.clear();renderLibrary();};
  bar.append(label,move,get,clear,downloadMeter(bundle));
  if(transfers.has(bundle.url))downloads.get(bundle.url)(transfers.get(bundle.url));
}
function folderDialog(title,copy,initial,choices,submit){
  show('<h2></h2><p class="modalcopy"></p><div class="folderChoices"></div><form id="folderForm"><label class="field" for="folderName">Nom du dossier</label><input id="folderName" class="textfield" maxlength="80" required autocomplete="off"><div class="actions"><button class="primary" id="folderOk">Valider</button></div><p class="notice" role="status" id="folderMsg"></p></form>');
  $('modalContent').querySelector('h2').textContent=title;$('modalContent').querySelector('.modalcopy').textContent=copy;
  $('folderName').value=initial||'';
  const box=$('modalContent').querySelector('.folderChoices');
  for(const name of choices){const b=node('button',name,'chip');b.type='button';b.onclick=()=>{$('folderName').value=name;$('folderForm').requestSubmit();};box.append(b);}
  box.hidden=!choices.length;
  setTimeout(()=>$('folderName').focus(),30);
  $('folderForm').onsubmit=async e=>{
    e.preventDefault();const name=$('folderName').value.trim();if(!name)return;
    $('folderOk').disabled=true;$('folderMsg').textContent='Rangement…';
    try{await submit(name);modal.close();await reloadJobs();}
    catch(err){$('folderMsg').textContent=err.message;$('folderOk').disabled=false;}
  };
}
function moveDialog(items){
  const current=new Set(items.map(({job,clip})=>folderOf(job,clip)));
  const choices=folders().map(f=>f.name).filter(n=>!(current.size===1&&current.has(n)));
  folderDialog('Ranger '+items.length+' clip'+(items.length>1?'s':''),'Choisis un dossier existant ou tape le nom d’un nouveau dossier. Les fichiers sont aussi déplacés dans le Finder.','',choices,
    async name=>{await api('/api/clips/move',{folder:name,clips:items.map(({job,clip})=>({job:job.id,clip:clip.id}))});picked.clear();});
}
function renameDialog(folder){
  folderDialog('Renommer le dossier','Le dossier est aussi renommé dans le Finder.',folder.name,[],
    async name=>{if(name!==folder.name)await api('/api/folders/rename',{from:folder.name,to:name});});
}
async function openFolder(name){
  try{await api('/api/folders/open',name?{folder:name}:{});}catch(e){status(e.message,true);}
}
function renderLibrary() {
  const list=$('clipList'), groups=folders();
  const total=groups.reduce((n,f)=>n+f.items.length,0);
  $('count').textContent=String(total);
  $('clipCount').textContent=groups.length+' dossier'+(groups.length>1?'s':'')+' · '+total+' clip'+(total>1?'s':'');
  if (!total) {renderSelection();return;}
  list.replaceChildren();downloads.clear();
  for (const folder of groups) {
    const group=node('section',undefined,'jobGroup folder'), head=node('div',undefined,'jobHead');
    const all=node('input');all.type='checkbox';all.className='pick';all.setAttribute('aria-label','Sélectionner tout le dossier '+folder.name);
    const keys=folder.items.map(({job,clip})=>refOf(job,clip)), n=keys.filter(k=>picked.has(k)).length;
    all.checked=n===keys.length;all.indeterminate=n>0&&n<keys.length;
    all.onchange=()=>{for(const k of keys)all.checked?picked.add(k):picked.delete(k);renderLibrary();};
    const toggle=node('button',collapsed.has(folder.name)?'▸':'▾','fold');toggle.setAttribute('aria-label','Replier ou déplier');
    toggle.onclick=()=>{collapsed.has(folder.name)?collapsed.delete(folder.name):collapsed.add(folder.name);renderLibrary();};
    const info=node('div',undefined,'jobInfo');
    const title=node('b');title.append(document.createTextNode('📁 '+folder.name));info.append(title);
    const running=folder.items.some(({job})=>job.status==='running');
    info.append(node('small',folder.items.length+' clip'+(folder.items.length>1?'s':'')+(running?' · montage en cours…':'')));
    head.append(all,toggle,info);
    const ready=folder.items.filter(({job})=>job.status==='done');
    let meter=null;
    if(ready.length>1){
      const bundle={url:archiveURL(ready,folder.name),name:'le dossier '+folder.name};
      const get=node('button','Tout télécharger','all');
      get.onclick=()=>saveFile({url:bundle.url,fileName:folder.name+'.zip'});
      head.append(get);
      meter=downloadMeter(bundle);
    }
    const rename=node('button','Renommer');rename.onclick=()=>renameDialog(folder);rename.disabled=running;
    const finder=node('button','Ouvrir dans le Finder');finder.onclick=()=>openFolder(folder.name);
    head.append(rename,finder);if(meter)head.append(meter);
    group.append(head);
    if(!collapsed.has(folder.name))for (const {job,clip} of folder.items) {
      const key=refOf(job,clip), row=node('article',undefined,'clip'+(picked.has(key)?' picked':''));
      const box=node('input');box.type='checkbox';box.className='pick';box.checked=picked.has(key);box.setAttribute('aria-label','Sélectionner '+clip.name);
      box.onchange=()=>{box.checked?picked.add(key):picked.delete(key);row.classList.toggle('picked',box.checked);renderSelection();
        const n=keys.filter(k=>picked.has(k)).length;all.checked=n===keys.length;all.indeterminate=n>0&&n<keys.length;};
      const play=node('button','Aperçu');play.onclick=()=>preview(clip);
      const file=node('a','Télécharger');file.href=clip.url;file.download=(clip.name||clip.id)+'.mp4';
      file.onclick=e=>{if(e.metaKey||e.ctrlKey||e.shiftKey||e.altKey)return;e.preventDefault();saveClip(clip);};
      const state=published.get(key);
      const publish=node('button',state==='PUBLISH_COMPLETE'?'Publié ✓':state&&state!=='FAILED'?'Publication…':'Publier sur TikTok','post'+(state==='PUBLISH_COMPLETE'?' done':''));
      publish.onclick=()=>openPublish(job,clip);
      const title=node('div',undefined,'clipTitle');title.append(node('b',clip.name));
      const meta=[];
      if(folder.jobs.size>1)meta.push(jobLabel(job));
      if(Number.isFinite(clip.start)&&Number.isFinite(clip.end))meta.push(timestamp(clip.start)+' – '+timestamp(clip.end)+' dans la source');
      if(job.highlights&&Number.isFinite(clip.score))meta.push('Intérêt IA '+clip.score+' / 100');
      if(meta.length)title.append(node('small',meta.join(' · ')));
      if(clip.excerpt)title.append(node('q',clip.excerpt,'excerpt'));
      row.append(box,title,play,file,publish,downloadMeter(clip));group.append(row);
    }
    list.append(group);
  }
  renderSelection();
  for(const [url,state] of transfers)downloads.get(url)?.(state);
}
async function watch(id) {
  activeJob=id;$('generate').disabled=true;$('generate').setAttribute('aria-busy','true');
  try {
    for (;;) {
      let job;
      try{job=await api('/api/jobs/'+encodeURIComponent(id));}
      catch(e){
        if(e.httpStatus===404)throw e;
        status('Connexion au studio interrompue. Reprise du suivi en cours…',true);await sleep(4000);continue;
      }
      job.id=id;
      const before=jobs.get(id);
      jobs.set(id,job);showGeneration(job);
      const count=job.clips?.length||0;
      if(count!==(before?.clips?.length||0)||job.status!==before?.status){
        renderLibrary();
        if(count&&!selected)preview(job.clips[0]);
      }
      if (job.status==='done') break;
      if (job.status==='error') break;
      await sleep(1000);
    }
  } catch(e) {status(e.message,true);}
  finally {activeJob=null;$('generate').disabled=false;$('generate').removeAttribute('aria-busy');}
}
$('editor').onsubmit=async e=>{
  e.preventDefault();if(!local){launchLocal();return;} if(activeJob)return;
  $('generate').disabled=true;showGeneration({stage:'queued',progress:0,message:'Création du montage…'});
  try {
    const job=await api('/api/jobs',{url:$('source').value.trim(),secondary_url:$('layout').value==='double'?$('second').value.trim():'',seconds:Number(document.querySelector('[name=seconds]:checked').value),captions:$('captions').checked,highlights:document.querySelector('[name=mode]:checked').value==='highlights'});
    await watch(job.id);
  }catch(err){showGeneration({status:'error',message:err.message});$('generate').disabled=false;}
};
document.querySelectorAll('[name=mode]').forEach(input=>input.addEventListener('change',()=>{
  $('modeHint').textContent=input.value==='highlights'?'L’IA écoute, regarde et choisit automatiquement les passages les plus intéressants.':'La vidéo est découpée du début à la fin, dans l’ordre.';
}));
function applyAccount(account){
  $('accountName').textContent=account.connected&&account.creator ? '@'+account.creator.creator_username : 'Associer un compte';
}
function showAccount(account) {
  applyAccount(account);
  if(!account.configured){
    setupTikTok(account);
  } else if(!account.connected) {
    show('<h2>Associer TikTok</h2><p class="modalcopy">Autorisez Split Studio à publier les clips que vous sélectionnez sur votre compte.</p><a class="primary" href="/api/tiktok/connect">Continuer avec TikTok</a>');
  } else {
    show('<h2>Compte associé</h2><p class="modalcopy" id="accountInfo"></p><div class="actions"><a class="primary" id="reconnect" href="/api/tiktok/connect">Réassocier le compte</a><button class="secondary" id="disconnect">Dissocier le compte</button></div>');
    $('reconnect').hidden=!!account.creator && !account.error;
    $('accountInfo').textContent=account.creator?.creator_nickname || account.creator?.creator_username || account.error || 'TikTok';
    $('disconnect').onclick=async()=>{try{await api('/api/tiktok/disconnect',{});applyAccount({connected:false});modal.close();}catch(e){$('accountInfo').textContent=e.message;}};
  }
}
function setupTikTok(account){
  const uri=account.redirect_uri||location.origin+'/api/tiktok/callback';
  show(`<h2>Associer TikTok</h2><p class="modalcopy">À faire une seule fois. Split Studio publie via ta propre application TikTok for Developers.</p>
  <ol class="steps">
   <li>Ouvre <a href="https://developers.tiktok.com/apps/" target="_blank" rel="noopener">developers.tiktok.com</a>, connecte-toi et crée une application.</li>
   <li>Ajoute les produits <b>Login Kit</b> (plateforme Desktop) et <b>Content Posting API</b> avec <b>Direct Post</b> et la portée <code>video.publish</code>.</li>
   <li>Dans Login Kit, ajoute cette adresse de redirection :<div class="copyrow"><input id="ttUri" readonly class="textfield"><button type="button" class="secondary" id="ttCopy">Copier</button></div></li>
   <li>Colle ici la <b>Client key</b> et le <b>Client secret</b> de ton application.</li>
  </ol>
  <form id="ttForm"><label class="field" for="ttKey">Client key</label><input id="ttKey" class="textfield" autocomplete="off" spellcheck="false" required>
  <label class="field" for="ttSecret">Client secret</label><input id="ttSecret" class="textfield" type="password" autocomplete="off" required>
  <div class="actions"><button class="primary" id="ttSave">Enregistrer et me connecter à TikTok</button></div><p class="notice" role="status" id="ttMsg"></p></form>`);
  $('ttUri').value=uri;
  $('ttCopy').onclick=async()=>{$('ttUri').select();try{await navigator.clipboard.writeText(uri);}catch{document.execCommand?.('copy');}$('ttCopy').textContent='Copié ✓';};
  $('ttForm').onsubmit=async e=>{
    e.preventDefault();$('ttSave').disabled=true;$('ttMsg').textContent='Enregistrement…';
    try{await api('/api/tiktok/setup',{client_key:$('ttKey').value,client_secret:$('ttSecret').value});
      if(window.pywebview?.api?.open_url)connectInBrowser();else location.href='/api/tiktok/connect';}
    catch(err){$('ttMsg').textContent=err.message;$('ttSave').disabled=false;}
  };
}
// ---- Published state & remembered hashtags ----
const published=new Map();
async function loadPosts(){try{const r=await api('/api/tiktok/posts');published.clear();for(const p of r.posts)published.set(p.job+':'+p.clip,p.state);}catch{}}
const store={get(k){try{return localStorage.getItem(k)||'';}catch{return '';}},set(k,v){try{localStorage.setItem(k,v);}catch{}}};
function defaultCaption(job,clip){
  const tags=store.get('splitstudio.hashtags');
  return [folderOf(job,clip)+' · '+clip.name,tags].filter(Boolean).join(' ').slice(0,2200);
}
const errorModal=e=>{show('<h2>TikTok</h2><p class="modalcopy" id="tiktokError"></p>');$('tiktokError').textContent=e.message;};
$('account').onclick=async()=>{if(!local){launchLocal();return;}try{showAccount(await api('/api/tiktok/account'));}catch(e){errorModal(e);}};
async function openPublish(job,clip) {
  let account;
  try {account=await api('/api/tiktok/account');}catch(e){errorModal(e);return;}
  if(!account.connected||!account.configured){showAccount(account);return;}
  if(!account.creator||account.error){showAccount(account);return;}
  const creator=account.creator;
  const version=show(`<h2>Publier sur TikTok</h2><p class="modalcopy" id="creator"></p><video controls playsinline id="postPreview"></video>
  <form id="postForm"><label class="field" for="postTitle">Légende</label><textarea id="postTitle" rows="2" maxlength="2200"></textarea><label class="field" for="privacy">Visibilité</label><select id="privacy" required><option value="">Choisir la visibilité</option></select>
  <h3>Interactions autorisées</h3><div id="perms"></div><h3>Contenu commercial</h3><label class="check"><input type="checkbox" id="commercial">Ce clip fait la promotion d’une marque ou d’un produit</label><div id="brands" hidden><label class="check"><input type="checkbox" id="organic">Ma propre marque</label><label class="check"><input type="checkbox" id="branded">Partenariat avec une autre marque</label><small id="commercialHint"></small></div><label class="check"><input type="checkbox" required id="consent"><span>J’accepte la publication et la <a href="https://www.tiktok.com/legal/page/global/music-usage-confirmation/en" target="_blank" rel="noopener">confirmation d’utilisation musicale</a><span id="brandTerms" hidden> ainsi que la <a href="https://www.tiktok.com/legal/page/global/bc-policy/en" target="_blank" rel="noopener">politique de contenu de marque</a></span>.</span></label>
  <div class="actions"><button class="primary" id="postSubmit">Publier ce clip</button></div><p class="notice" role="status" id="postResult"></p></form>`);
  $('creator').textContent='Compte de publication · '+creator.creator_nickname+' (@'+creator.creator_username+')';
  $('postPreview').src=clip.url;
  $('postTitle').value=defaultCaption(job,clip);
  for (const val of creator.privacy_level_options||[]) $('privacy').append(new Option(labels[val]||val,val));
  for(const [key,label] of Object.entries({comment:'Commentaires',duet:'Duo',stitch:'Collage'})){
    const l=node('label',undefined,'check'),c=node('input');c.type='checkbox';c.id='allow_'+key;c.disabled=!!creator[key+'_disabled'];l.append(c,document.createTextNode(label));$('perms').append(l);
  }
  function commercialChange(){
    const on=$('commercial').checked;
    $('brands').hidden=!on;
    if(!on){$('branded').checked=false;$('organic').checked=false;}
    const branded=$('branded').checked;
    $('brandTerms').hidden=!branded;
    $('commercialHint').textContent=branded?'Mention « Partenariat rémunéré ». Un partenariat doit être visible au-delà de « Moi uniquement ».':$('organic').checked?'Mention « Contenu promotionnel ».':'Sélectionnez le type de promotion.';
    const priv=Array.from($('privacy').options).find(opt=>opt.value==='SELF_ONLY');
    if(priv){priv.disabled=branded;if(branded&&priv.selected)$('privacy').value='';}
  }
  for(const id of ['commercial','organic','branded'])$(id).onchange=commercialChange;
  $('postPreview').onloadedmetadata=()=>{
    if($('postPreview').duration>(creator.max_video_post_duration_sec||Infinity)+0.1){
      $('postResult').textContent='Ce clip dépasse la durée autorisée pour ce compte ('+creator.max_video_post_duration_sec+' s).';$('postSubmit').disabled=true;
    }
  };
  $('postForm').onsubmit=async e=>{
    e.preventDefault();
    if($('commercial').checked&&!$('branded').checked&&!$('organic').checked){$('postResult').textContent='Sélectionnez le type de promotion.';return;}
    $('postSubmit').disabled=true;$('postResult').textContent='Envoi du clip…';
    let accepted=false;
    try{
      const result=await api('/api/tiktok/publish',{job_id:job.id,clip_id:clip.id,title:$('postTitle').value,privacy_level:$('privacy').value,allow_comment:$('allow_comment').checked,allow_duet:$('allow_duet').checked,allow_stitch:$('allow_stitch').checked,brand_organic_toggle:$('organic').checked,brand_content_toggle:$('branded').checked,consent:$('consent').checked,creator_username:creator.creator_username});
      accepted=true;
      const tags=($('postTitle').value.match(/#[\p{L}\p{N}_]+/gu)||[]).join(' ');
      store.set('splitstudio.hashtags',tags);
      published.set(job.id+':'+clip.id,'PROCESSING');renderLibrary();
      while(modal.open&&modalVersion===version){
        $('postResult').textContent='Clip envoyé. TikTok traite la publication…';
        await sleep(5500);const info=await api('/api/tiktok/status/'+encodeURIComponent(result.publish_id));
        if(!modal.open||modalVersion!==version)return;
        if(info.status==='PUBLISH_COMPLETE'){$('postResult').textContent='TikTok a confirmé la publication.';published.set(job.id+':'+clip.id,'PUBLISH_COMPLETE');renderLibrary();return;}
        if(info.status==='FAILED'){throw Error('Publication refusée par TikTok · '+(info.fail_reason||'raison inconnue'));}
      }
    }catch(err){if(modalVersion===version){$('postResult').textContent=err.message;$('postSubmit').disabled=accepted;}}
  };
}
function desktopMode(){document.documentElement.classList.add('desktop');}
// In the Mac app, web pages open in the default browser (the app window cannot show them).
document.addEventListener('click',e=>{
  const a=e.target.closest?.('a[href]');const bridge=window.pywebview?.api;
  if(!a||!bridge?.open_url)return;
  const href=a.getAttribute('href');
  if(href==='/api/tiktok/connect'){e.preventDefault();connectInBrowser();return;}
  if(/^https?:\/\//.test(href)&&!href.startsWith(location.origin)){e.preventDefault();bridge.open_url(href);}
},true);
async function connectInBrowser(){
  await window.pywebview.api.open_url(location.origin+'/api/tiktok/connect?from=app');
  const version=show('<h2>Connexion à TikTok</h2><p class="modalcopy">Termine la connexion dans ton navigateur, puis reviens ici : Split Studio détecte automatiquement ton compte.</p><progress></progress><div class="actions"><button class="secondary" id="ttRetry">Rouvrir la page de connexion</button></div>');
  $('ttRetry').onclick=()=>window.pywebview.api.open_url(location.origin+'/api/tiktok/connect?from=app');
  while(modal.open&&modalVersion===version){
    await sleep(2000);
    try{const acc=await api('/api/tiktok/account');
      if(acc.connected&&modalVersion===version){applyAccount(acc);
        show('<h2>TikTok associé ✓</h2><p class="modalcopy" id="ttOk"></p>');
        $('ttOk').textContent='Compte '+(acc.creator?'@'+acc.creator.creator_username:'TikTok')+' prêt. Clique sur « Publier sur TikTok » à côté d’un clip pour le publier.';return;}
    }catch{}
  }
}
window.addEventListener('pywebviewready',desktopMode);if(window.pywebview)desktopMode();
(async()=>{
  try{const h=await api('/api/health');local=h.ok===true;if(h.version){const tag=node('span','v'+h.version,'version');document.querySelector('.brand').after(tag);}}catch{}
  if(!local)return;
  try{applyAccount(await api('/api/tiktok/account'));}catch{}
  const params=new URLSearchParams(location.search);
  if(params.get('tiktok')==='error')errorModal(Error(params.get('message')||'Association interrompue.'));
  if(params.get('tiktok')==='connected'){
    try{const acc=await api('/api/tiktok/account');applyAccount(acc);
      show('<h2>TikTok associé ✓</h2><p class="modalcopy" id="ttOk"></p>');
      $('ttOk').textContent='Compte '+(acc.creator?'@'+acc.creator.creator_username:'TikTok')+' prêt. Clique sur « Publier sur TikTok » à côté d’un clip pour le publier.';}catch{}
  }
  if(params.has('tiktok'))history.replaceState(null,'',location.pathname);
  await loadPosts();
  try{
    const history=await api('/api/jobs');for(const job of history.jobs)jobs.set(job.id,job);renderLibrary();
    const running=history.jobs.find(j=>j.status==='running');if(running){showGeneration(running);watch(running.id);}else if(history.jobs[0])showGeneration(history.jobs[0]);
  }catch{}
})();
