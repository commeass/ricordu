const $ = s => document.querySelector(s);
const state = { folder:null, event:"", beats:4, rhythm:"fixe", order:"chrono", captions:false, hw:true,
                videoAudio:"cut", sceneThr:0.5, endPhoto:null, catalog:[], selected:new Set(), suggested:new Set(), mood:"tous" };

// ---- helpers ----
const fmtDur = s => `${Math.floor(s/60)}:${String(s%60).padStart(2,"0")}`;
function seg(el, cb){ el.querySelectorAll("button").forEach(b=>b.onclick=()=>{
  el.querySelectorAll("button").forEach(x=>x.classList.remove("on")); b.classList.add("on"); cb(b.dataset.v);
});}

// ---- controls ----
$("#dur").oninput = e => { $("#durVal").textContent = fmtDur(+e.target.value); updateMusicBudget(); };
$("#vid").oninput = e => $("#vidVal").textContent = e.target.value + "%";
$("#scenethr").oninput = e => { state.sceneThr = +e.target.value; $("#sceneVal").textContent = (+e.target.value).toFixed(2); };
seg($("#rhythm"), v => { if(v==="dyn"){ state.rhythm="dynamique"; } else if(v==="module"){ state.rhythm="module"; } else if(v==="recit"){ state.rhythm="recit"; } else { state.rhythm="fixe"; state.beats=+v; } updateMusicBudget(); });
seg($("#order"),  v => state.order = v);
seg($("#videoaudio"), v => state.videoAudio = v);
$("#capSw").onclick = () => { state.captions=!state.captions; $("#capSw").classList.toggle("on", state.captions); };
$("#hwSw").onclick  = () => { state.hw=!state.hw; $("#hwSw").classList.toggle("on", state.hw); };

// ---- dossier ----
$("#pickBtn").onclick = async () => {
  const r = await (await fetch("/api/pick-folder",{method:"POST"})).json();
  if(!r.path) return;
  state.folder = r.path;
  state.event = r.path.split("/").pop();
  $("#folderPath").textContent = r.path;
  $("#folderPath").classList.remove("empty");
  $("#runBtn").disabled = false;
  if(!$("#title").value) $("#title").value = state.event;
  // suggestion musicale selon l'événement
  const s = await (await fetch("/api/suggest",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({event:state.event})})).json();
  state.suggested = new Set(s.suggested);
  state.selected = new Set(s.suggested.slice(0,1));   // pré-sélectionne la meilleure
  $("#musicHint").textContent = "· suggérée pour « "+state.event+" »";
  renderTracks();
};

// ---- musique ----
async function loadCatalog(){
  const r = await (await fetch("/api/catalog")).json();
  state.catalog = r.tracks;
  const moods = ["tous", ...new Set(r.tracks.map(t=>t.mood))];
  $("#moods").innerHTML = moods.map(m=>`<div class="chip ${m==='tous'?'on':''}" data-m="${m}">${m}</div>`).join("");
  $("#moods").querySelectorAll(".chip").forEach(c=>c.onclick=()=>{
    state.mood=c.dataset.m; $("#moods").querySelectorAll(".chip").forEach(x=>x.classList.remove("on"));
    c.classList.add("on"); renderTracks();
  });
  renderTracks();
}
const MOOD_LABEL={joyeux:"Joyeux",fun:"Fun",tendre:"Tendre",epique:"Épique",chill:"Chill"};
function renderTracks(){
  const list = state.catalog.filter(t=>state.mood==="tous"||t.mood===state.mood);
  $("#tracks").innerHTML = list.map(t=>{
    const sel=state.selected.has(t.file), sug=state.suggested.has(t.file);
    return `<div class="track ${sel?'sel':''}" data-f="${t.file}">
      ${sug?'<span class="badge">suggéré</span>':''}
      <button class="play" data-f="${t.file}">▶</button>
      <div class="meta"><div class="t">${t.title}</div>
        <div class="s">${MOOD_LABEL[t.mood]||t.mood} · ${t.bpm} BPM · <b>${fmtDur(t.duration||0)}</b></div></div>
      <span class="check">✓</span></div>`;
  }).join("");
  $("#tracks").querySelectorAll(".track").forEach(el=>{
    el.onclick = e => {
      if(e.target.classList.contains("play")) return;
      const f=el.dataset.f; state.selected.has(f)?state.selected.delete(f):state.selected.add(f);
      el.classList.toggle("sel"); updateMusicBudget();
    };
  });
  $("#tracks").querySelectorAll(".play").forEach(b=>b.onclick=e=>{
    e.stopPropagation(); const a=$("#audio");
    if(a.dataset.f===b.dataset.f && !a.paused){ a.pause(); b.textContent="▶"; return; }
    document.querySelectorAll(".play").forEach(x=>x.textContent="▶");
    a.src="/"+b.dataset.f; a.dataset.f=b.dataset.f; a.play(); b.textContent="❚❚";
    a.onended=()=>b.textContent="▶";
  });
  updateMusicBudget();
}

// ---- budget musique : la durée de la musique contraint la durée du montage ----
const TITLE_END = 5;   // ~ titre + carton de fin qui s'ajoutent au corps (s)
const XFADE = 2;       // fondu enchaîné entre 2 pistes (build_music_mix : acrossfade d=2)
function musicAvail(){  // durée réellement disponible une fois les pistes enchaînées
  const ds = [...state.selected].map(f => (state.catalog.find(x=>x.file===f)||{}).duration||0).filter(Boolean);
  if(!ds.length) return 0;
  return Math.max(0, ds.reduce((a,b)=>a+b,0) - XFADE*(ds.length-1));
}
function updateMusicBudget(){
  const el = $("#musicBudget"); if(!el) return;
  const n = state.selected.size, avail = musicAvail();
  const target = +$("#dur").value, montage = target + TITLE_END;
  if(!n){ el.className="mbudget"; el.innerHTML=""; return; }
  if(state.rhythm==="module"){
    const list = [...state.selected].map(f=>{const t=state.catalog.find(x=>x.file===f);return t?fmtDur(t.duration):'';}).filter(Boolean).join(' · ');
    el.className="mbudget show";
    el.innerHTML=`<div class="mb-top"><span class="mb-ico">🎚️</span>
        <span class="mb-head">Mode Module — ${n} musique${n>1?'s':''}</span></div>
      <div class="mb-msg">Chaque musique remplit sa propre section (elle peut boucler à l'intérieur) :
        la contrainte de durée globale ne s'applique pas. Durées : <b>${list}</b>.</div>`;
    return;
  }
  const deficit = montage - avail;                          // >0 = il manque de la musique
  const pistes = n>1 ? ` (${n} pistes, fondus −${XFADE*(n-1)}s)` : "";
  if(deficit <= 0){
    el.className="mbudget show ok";
    el.innerHTML=`<div class="mb-top"><span class="mb-ico">✅</span>
        <span class="mb-head">La musique couvre tout le montage</span></div>
      <div class="mb-bar"><div class="mb-fill" style="width:${Math.round(montage/avail*100)}%"></div></div>
      <div class="mb-msg">🎵 <b>${fmtDur(avail)}</b> de musique${pistes} · montage ≈ <b>${fmtDur(montage)}</b>
        (cible ${fmtDur(target)} + ~${TITLE_END}s titre/fin) · marge <b>${fmtDur(avail-montage)}</b>.
        Elle se termine en fondu, sans boucle.</div>`;
  } else {
    const fit = Math.max(30, Math.min(240, Math.round(avail - TITLE_END)));
    el.className="mbudget show warn";
    el.innerHTML=`<div class="mb-top"><span class="mb-ico">⚠️</span>
        <span class="mb-head">Il manque ${fmtDur(deficit)} de musique</span></div>
      <div class="mb-bar"><div class="mb-fill" style="width:${Math.round(avail/montage*100)}%"></div></div>
      <div class="mb-msg">🎵 <b>${fmtDur(avail)}</b> dispo${pistes} · montage ≈ <b>${fmtDur(montage)}</b>
        (cible ${fmtDur(target)} + ~${TITLE_END}s titre/fin). Sans plus de musique, elle <b>bouclera</b> (raccord audible).
        Ajoute une piste, choisis-en une plus longue, ou réduis la durée :</div>
      <button class="mb-fix" id="mbFix">⤵ Caler le montage sur la musique (${fmtDur(fit)})</button>`;
    const fix = $("#mbFix");
    if(fix) fix.onclick = ()=>{ $("#dur").value=fit; $("#durVal").textContent=fmtDur(fit); updateMusicBudget(); };
  }
}

// ---- run ----
$("#runBtn").onclick = () => {
  if(!state.folder) return;
  const opts = { folder:state.folder, title:$("#title").value.trim()||state.event,
    end_text:$("#endtext").value.trim(), end_photo:state.endPhoto,
    target:+$("#dur").value, beats_per_clip:state.beats,
    rhythm:state.rhythm, video_share:(+$("#vid").value)/100, order:state.order, captions:state.captions,
    hardware_accel:state.hw, video_audio:state.videoAudio, scene_threshold_ai:state.sceneThr, music:[...state.selected] };
  $("#runBtn").disabled = true; $("#runBtn").textContent="Montage en cours…";
  $("#progress").style.display="block"; $("#reports").innerHTML=""; $("#log").innerHTML="";
  $("#progress").scrollIntoView({behavior:"smooth"});
  fetch("/api/run",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(opts)})
    .then(r=>r.json()).then(r=>{ if(r.error){ alert(r.error); resetRun(); } else listen(); });
};
function resetRun(){ $("#runBtn").disabled=false; $("#runBtn").textContent="Créer le montage"; }

function setStep(stage,done){
  const map={scan:0,ai:1,render:2}; const ss=$("#stepper").children;
  [...ss].forEach((s,i)=>{ s.classList.remove("act","done");
    if(i<map[stage]||done) s.classList.add("done"); if(i===map[stage]&&!done) s.classList.add("act"); });
}
function listen(){
  const es = new EventSource("/api/events");
  es.onmessage = e => {
    const ev = JSON.parse(e.data);
    if(ev.type==="stage") setStep(ev.stage,false);
    if(ev.type==="progress"){ $("#barFill").style.width=ev.value+"%"; $("#detail").textContent=ev.detail||""; }
    if(ev.type==="log"){ const l=$("#log"); l.innerHTML+=ev.line+"<br>"; l.scrollTop=l.scrollHeight; }
    if(ev.type==="report") report(ev.name, ev.data);
    if(ev.type==="done"){ setStep("render",true); $("#barFill").style.width="100%";
      $("#detail").textContent="Terminé ✓"; resetRun(); es.close(); loadSelection(); }
    if(ev.type==="error"){ $("#detail").textContent="Erreur : "+ev.message; resetRun(); es.close(); }
  };
}

// ---- rapports ----
function card(title, inner){ return `<div class="card"><h2>${title}</h2>${inner}</div>`; }
function stat(v,k){ return `<div class="stat"><div class="v">${v}</div><div class="k">${k}</div></div>`; }
function report(name, d){
  const R=$("#reports");
  if(name==="scan"){
    R.insertAdjacentHTML("beforeend", card("Rapport — Scan",
      `<div class="rep">${stat(d.photos,"photos")}${stat(d.videos,"vidéos")}
       ${stat(d.rush+"s","de rush")}${stat(d.span||"—","période")}</div>`));
  }
  if(name==="selection"){
    const mx=Math.max(1,...Object.values(d.hist));
    const hist=Object.entries(d.hist).map(([k,v])=>
      `<div class="b" style="height:${Math.round(v/mx*100)}%" title="${v}"><span>${k}</span></div>`).join("");
    const th=d.highlights.map(h=>`<div class="th"><img loading="lazy"
        src="/api/thumb?file=${encodeURIComponent(h.file)}"><b>${(h.score||0).toFixed(1)}</b></div>`).join("");
    const mom=d.moments.map(m=>`<div class="m"><b>${(m.dur||0)}s</b> ${m.caption||"clip vidéo"}</div>`).join("")
      || '<div class="muted">aucun</div>';
    R.insertAdjacentHTML("beforeend", card("Rapport — Sélection best-of",
      `<div class="rep" style="margin-bottom:18px">${stat(d.kept+"/"+d.scored,"plans gardés")}
        ${stat(d.photos,"photos")}${stat(d.videos,"moments vidéo")}${stat(d.dropped,"écartés")}</div>
       <div class="muted" style="margin-bottom:6px">Répartition des scores photos</div>
       <div class="hist" style="margin-bottom:24px">${hist}</div>
       <div class="muted" style="margin-bottom:4px">Sélection (meilleurs plans)</div>
       <div class="thumbs">${th}</div>
       <div class="muted" style="margin:18px 0 4px">Moments vidéo (son réel)</div>
       <div class="moments">${mom}</div>`));
  }
  if(name==="final"){
    const sync = d.sync_ms!=null ? `${d.sync_ms} ms (max ${d.sync_max})` : "—";
    R.insertAdjacentHTML("beforeend", card("Montage final",
      `<div style="margin-bottom:14px">
        <span class="pill">Durée <b>${fmtDur(Math.round(d.duration||0))}</b></span>
        <span class="pill">Résolution <b>${d.resolution||"—"}</b></span>
        <span class="pill">Tempo <b>${d.bpm||"—"} BPM</b></span>
        <span class="pill">Sync cut↔beat <b>${sync}</b></span></div>
       <video controls src="/media/montage.mp4?t=${Date.now()}"></video>
       <div style="margin-top:12px"><a href="/media/montage.mp4" download="montage.mp4">⬇ Télécharger le montage</a></div>`));
    $("#reports").scrollIntoView({behavior:"smooth"});
  }
}

// ---- lecture INLINE d'un extrait (dans la vignette même, en boucle) ----
function playInline(cell, src, start, dur){
  const ex=cell.querySelector("video");
  const showImg=()=>{ const im=cell.querySelector("img"); if(im) im.style.visibility="visible"; };
  if(ex){ ex.remove(); showImg(); return; }              // re-clic = stop
  const stop=start+dur, v=document.createElement("video");
  v.src=src+`#t=${start}`; v.playsInline=true; v.className="inlinevid";
  v.onloadedmetadata=()=>{ try{v.currentTime=start;}catch(e){} v.play().catch(()=>{}); };
  v.ontimeupdate=()=>{ if(v.currentTime>=stop || v.currentTime<start-0.2) v.currentTime=start; };
  v.onclick=e=>{ e.stopPropagation(); v.remove(); showImg(); };
  const im=cell.querySelector("img"); if(im) im.style.visibility="hidden";
  cell.appendChild(v);
}

// ---- découpe manuelle d'un extrait ----
let cutFile=null, cutIn=null, cutOut=null;
function openCut(file, src){
  cutFile=file; cutIn=cutOut=null;
  const v=$("#cutvid"); v.src=src; $("#cutrange").textContent="scrub la vidéo, puis marque début / fin";
  $("#cutmodal").classList.add("on");
}
const cutFmt=t=>t==null?"—":t.toFixed(1)+"s";
function updCut(){ $("#cutrange").textContent=`début ${cutFmt(cutIn)} · fin ${cutFmt(cutOut)}`; }
$("#markIn").onclick=()=>{ cutIn=$("#cutvid").currentTime; if(cutOut!=null&&cutOut<=cutIn)cutOut=null; updCut(); };
$("#markOut").onclick=()=>{ cutOut=$("#cutvid").currentTime; updCut(); };
$("#cutClose").onclick=()=>{ const v=$("#cutvid"); v.pause(); v.removeAttribute("src"); v.load(); $("#cutmodal").classList.remove("on"); };
$("#cutmodal").onclick=e=>{ if(e.target.id==="cutmodal") $("#cutClose").click(); };
$("#addClip").onclick=()=>{
  if(cutIn==null||cutOut==null||cutOut<=cutIn){ alert("Marque un début ET une fin (la fin après le début)."); return; }
  fetch("/api/add-clip",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({file:cutFile,start:cutIn,end:cutOut})})
    .then(r=>r.json()).then(r=>{ if(r.error){alert(r.error);return;} $("#cutClose").click(); loadSelection(); });
};

// ---- éditeur de sélection ----
function loadSelection(){
  fetch("/api/selection").then(r=>r.json()).then(d=>{
    if(!d.clips || !d.clips.length){ $("#editor").innerHTML=""; return; }
    if(d.title) $("#title").value=d.title;
    if(d.end_text && !$("#endtext").value) $("#endtext").value=d.end_text;
    if(d.end_photo) state.endPhoto=d.end_photo;
    const days=[...new Set(d.clips.map(c=>(c.when||'').slice(0,5)).filter(Boolean))];
    const dayChips = days.length>1
      ? `<div class="moods" id="dayf"><div class="chip on" data-d="tous">tous les jours</div>`
        + days.map(dd=>`<div class="chip" data-d="${dd}">${dd}</div>`).join("") + `</div>` : "";
    const esc=s=>(s||'').replace(/"/g,'&quot;');
    const cells = d.clips.map(c=>`
      <div class="cell ${c.include?'on':''} ${c.file===state.endPhoto?'endsel':''}" data-id="${c.id}" data-day="${(c.when||'').slice(0,5)}" data-file="${esc(c.file)}">
        <img loading="lazy" src="${c.thumb}">
        <span class="sc">${c.score}</span>
        ${c.when?`<span class="when">${c.when}</span>`:''}
        ${c.type==='video'?`<span class="vtag">▶ ${c.range||''}</span>
          <button class="vplay" data-src="${c.src}" data-start="${c.start}" data-dur="${c.dur}">▶</button>
          <button class="cutbtn" data-file="${esc(c.file)}" data-src="${c.src}">✂️ découper</button>`
        :`<button class="endbtn" title="Choisir comme photo de fin">🏁 fin</button>`}
        <span class="tick">✓</span></div>`).join("");
    $("#editor").innerHTML = `<div class="card">
      <h2>Sélection — clique pour inclure / exclure <span class="muted" id="selCount"></span></h2>
      <div class="muted" style="margin-bottom:14px">Ordre chronologique (heure affichée sur chaque vignette). ▶ visionne l'extrait sur place. Ajuste puis relance.</div>
      ${dayChips}
      <div class="cells">${cells}</div>
      <div style="display:flex;gap:12px;margin-top:20px">
        <button class="btn-primary" id="redo" style="flex:1">Relancer le montage</button>
        <button class="btn" id="saveProj">💾 Sauvegarder</button>
      </div></div>`;
    $("#editor").querySelectorAll(".cell").forEach(el=>el.onclick=ev=>{
      if(ev.target.classList.contains("vplay")) return;
      el.classList.toggle("on"); selCount();
    });
    $("#editor").querySelectorAll(".vplay").forEach(b=>b.onclick=ev=>{
      ev.stopPropagation(); playInline(b.closest(".cell"), b.dataset.src, +b.dataset.start, +b.dataset.dur);
    });
    $("#editor").querySelectorAll(".endbtn").forEach(b=>b.onclick=ev=>{
      ev.stopPropagation(); markEndPhoto(b.closest(".cell").dataset.file);
    });
    $("#editor").querySelectorAll(".cutbtn").forEach(b=>b.onclick=ev=>{
      ev.stopPropagation(); openCut(b.dataset.file, b.dataset.src);
    });
    const df=$("#dayf");
    if(df) df.querySelectorAll(".chip").forEach(ch=>ch.onclick=()=>{
      df.querySelectorAll(".chip").forEach(x=>x.classList.remove("on")); ch.classList.add("on");
      const sel=ch.dataset.d;
      $("#editor").querySelectorAll(".cell").forEach(el=>el.style.display=(sel==="tous"||el.dataset.day===sel)?"":"none");
    });
    $("#redo").onclick=redo; $("#saveProj").onclick=saveProject; selCount();
    $("#editor").scrollIntoView({behavior:"smooth"});
  });
}
function selCount(){ const on=document.querySelectorAll(".cell.on").length, t=document.querySelectorAll(".cell").length;
  const e=$("#selCount"); if(e) e.textContent=`· ${on}/${t} retenus`; }
function markEndPhoto(file){
  state.endPhoto = (state.endPhoto===file) ? null : file;   // re-clic = annule
  document.querySelectorAll(".cell").forEach(el=>el.classList.toggle("endsel", el.dataset.file===state.endPhoto));
}
function redo(){
  const inc={}; document.querySelectorAll(".cell").forEach(el=>inc[el.dataset.id]=el.classList.contains("on"));
  $("#redo").disabled=true; $("#redo").textContent="Re-montage…";
  $("#reports").innerHTML=""; $("#progress").style.display="block"; setStep("render",false);
  $("#barFill").style.width="70%"; $("#progress").scrollIntoView({behavior:"smooth"});
  fetch("/api/reselect",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({includes:inc, title:$("#title").value.trim(),
      end_text:$("#endtext").value.trim(), end_photo:state.endPhoto,
      rhythm:state.rhythm, beats_per_clip:state.beats, order:state.order,
      captions:state.captions, hardware_accel:state.hw, video_audio:state.videoAudio,
      scene_threshold_ai:state.sceneThr, music:[...state.selected]})})
    .then(r=>r.json()).then(r=>{ if(r.error){alert(r.error);$("#redo").disabled=false;$("#redo").textContent="Relancer le montage";} else listen(); });
}

// ---- projets ----
function saveProject(){
  const name=prompt("Nom du projet :", $("#title").value.trim()||"Mon montage"); if(!name) return;
  fetch("/api/save-project",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({name})})
    .then(r=>r.json()).then(r=>{ if(r.error)alert(r.error); else { loadProjects(); alert("Projet sauvegardé ✓"); } });
}
function loadProjects(){
  fetch("/api/projects").then(r=>r.json()).then(d=>{
    if(!d.projects || !d.projects.length){ $("#projCard").style.display="none"; return; }
    $("#projCard").style.display="block";
    $("#projList").innerHTML = d.projects.map(p=>`
      <div class="proj" data-name="${p.name}">
        <span class="pt">${p.title||p.name}</span>
        <span class="pm">${p.saved?p.saved+' · ':''}${p.clips} plans</span>
        <button class="pdel" data-name="${p.name}" title="Supprimer">🗑</button></div>`).join("");
    $("#projList").querySelectorAll(".proj").forEach(el=>el.onclick=ev=>{
      if(ev.target.classList.contains("pdel")) return; loadProject(el.dataset.name); });
    $("#projList").querySelectorAll(".pdel").forEach(b=>b.onclick=ev=>{ ev.stopPropagation(); deleteProject(b.dataset.name); });
  });
}
function deleteProject(name){
  if(!confirm(`Supprimer le projet « ${name} » ?`)) return;
  fetch("/api/delete-project",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({name})})
    .then(r=>r.json()).then(()=>loadProjects());
}
function loadProject(name){
  fetch("/api/load-project",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({name})})
    .then(r=>r.json()).then(r=>{ if(r.error){alert(r.error);return;}
      const s=r.settings||{};
      if(s.title!=null) $("#title").value=s.title;
      if(s.end_text!=null) $("#endtext").value=s.end_text||"";
      state.endPhoto = s.end_photo || null;
      if(s.target_duration){ $("#dur").value=s.target_duration; $("#durVal").textContent=fmtDur(+s.target_duration); }
      if(s.video_share!=null){ $("#vid").value=Math.round(s.video_share*100); $("#vidVal").textContent=Math.round(s.video_share*100)+"%"; }
      updateMusicBudget();
      $("#reports").innerHTML="";   // évite d'afficher un montage périmé après chargement
      loadSelection();
    });
}

loadCatalog();
loadProjects();
