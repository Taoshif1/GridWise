let lastResult = null;
const $ = (id) => document.getElementById(id);
const num = (id) => Number($(id).value);

function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>'"]/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[ch]));
}

function createHourRows() {
  const body = $('hoursBody');
  body.innerHTML = '';
  for (let h = 0; h < 24; h++) {
    const row = document.createElement('tr');
    row.innerHTML = `
      <td class="hour-cell">${String(h).padStart(2,'0')}:00</td>
      <td><input type="number" min="0" step="0.01" data-hour="${h}" data-field="demand_kwh"></td>
      <td><input type="number" min="0" step="0.01" data-hour="${h}" data-field="solar_kwh"></td>
      <td><input type="number" min="0" step="0.01" data-hour="${h}" data-field="tariff_bdt_per_kwh"></td>`;
    body.appendChild(row);
  }
}

function populate(data) {
  $('scenarioId').value = data.scenario_id || '';
  const notes = data.operator_notes || [];
  ['note1','note2','note3'].forEach((id,i) => $(id).value = notes[i] || '');
  updateNoteCount();
  const b = data.battery || {};
  $('capacity').value = b.capacity_kwh ?? '';
  $('initialEnergy').value = b.initial_energy_kwh ?? '';
  $('minimumEnergy').value = b.minimum_energy_kwh ?? '';
  $('maxCharge').value = b.max_charge_kwh_per_hour ?? '';
  $('maxDischarge').value = b.max_discharge_kwh_per_hour ?? '';
  const map = new Map((data.hours || []).map(x => [Number(x.hour), x]));
  document.querySelectorAll('#hoursBody input').forEach(input => {
    const h = Number(input.dataset.hour), field = input.dataset.field;
    input.value = map.get(h)?.[field] ?? '';
  });
}

function collect() {
  const notes = ['note1','note2','note3'].map(id => $(id).value.trim()).filter(Boolean);
  const hours = Array.from({length:24}, (_,h) => {
    const get = field => Number(document.querySelector(`#hoursBody input[data-hour="${h}"][data-field="${field}"]`).value);
    return {hour:h, demand_kwh:get('demand_kwh'), solar_kwh:get('solar_kwh'), tariff_bdt_per_kwh:get('tariff_bdt_per_kwh')};
  });
  return {
    scenario_id: $('scenarioId').value.trim(), operator_notes: notes, hours,
    battery: {
      capacity_kwh:num('capacity'), initial_energy_kwh:num('initialEnergy'), minimum_energy_kwh:num('minimumEnergy'),
      max_charge_kwh_per_hour:num('maxCharge'), max_discharge_kwh_per_hour:num('maxDischarge')
    }
  };
}

function updateNoteCount(){ $('noteCount').value = ['note1','note2','note3'].filter(id => $(id).value.trim()).length; }

async function loadSample(){
  const r = await fetch('/sample-request');
  if(!r.ok) throw new Error('Could not load sample request.');
  populate(await r.json());
}

function downloadJson(filename, data){
  const blob = new Blob([JSON.stringify(data,null,2)], {type:'application/json'});
  const url = URL.createObjectURL(blob); const a=document.createElement('a'); a.href=url; a.download=filename; a.click(); URL.revokeObjectURL(url);
}

function fmt(v,d=2){ return Number(v).toLocaleString(undefined,{maximumFractionDigits:d}); }

function renderResult(data){
  lastResult = data;
  $('placeholder').classList.add('hidden'); $('results').classList.remove('hidden');
  $('resultScenario').textContent = `Scenario ${data.scenario_id}`;
  $('totalCost').textContent = `৳${fmt(data.total_cost_bdt)}`;
  $('totalGrid').textContent = `${fmt(data.total_grid_kwh)} kWh`;
  $('peakGrid').textContent = `${fmt(data.peak_grid_kwh)} kWh`;
  $('summaryBox').textContent = data.plan_summary;
  $('rawJson').textContent = JSON.stringify(data,null,2);

  $('directiveList').innerHTML = data.directive_interpretation.map(d => {
    const adj = d.structured_adjustment === null ? 'No schedule adjustment' : JSON.stringify(d.structured_adjustment);
    return `<div class="directive"><div class="directive-head"><span class="directive-type">#${d.note_index+1} ${escapeHtml(d.directive_type)}</span><span class="applies ${d.applies?'':'no'}">${d.applies?'APPLIES':'NO OP'}</span></div><p>${escapeHtml(d.explanation)}</p><code>${escapeHtml(adj)}</code></div>`;
  }).join('');

  $('scheduleBody').innerHTML = data.hourly_plan.map(p => `<tr><td class="hour-cell">${String(p.hour).padStart(2,'0')}:00</td><td>${fmt(p.grid_kwh)}</td><td>${fmt(p.solar_used_kwh)}</td><td><span class="badge ${p.battery_action}">${p.battery_action}</span></td><td>${fmt(p.battery_kwh)}</td><td>${fmt(p.battery_energy_after_kwh)}</td></tr>`).join('');
  requestAnimationFrame(() => drawChart(data.hourly_plan));
  $('results').scrollIntoView({behavior:'smooth', block:'start'});
}

function drawChart(plan){
  const canvas=$('planChart'), box=canvas.parentElement, dpr=window.devicePixelRatio||1;
  const w=Math.max(300,box.clientWidth-24), h=Math.max(180,box.clientHeight-24); canvas.width=w*dpr; canvas.height=h*dpr; canvas.style.width=w+'px'; canvas.style.height=h+'px';
  const ctx=canvas.getContext('2d'); ctx.scale(dpr,dpr); ctx.clearRect(0,0,w,h);
  const pad={l:38,r:10,t:14,b:26}; const iw=w-pad.l-pad.r, ih=h-pad.t-pad.b;
  const series=[
    {key:'grid_kwh',color:'#66a5ff',label:'Grid'},
    {key:'solar_used_kwh',color:'#56d6a7',label:'Solar'},
    {key:'battery_energy_after_kwh',color:'#ffcf70',label:'Battery'}
  ];
  const max=Math.max(1,...plan.flatMap(p=>series.map(s=>Number(p[s.key])||0)))*1.08;
  ctx.strokeStyle='#203654';ctx.lineWidth=1;ctx.fillStyle='#7890ad';ctx.font='10px system-ui';
  for(let i=0;i<=4;i++){const y=pad.t+ih*i/4;ctx.beginPath();ctx.moveTo(pad.l,y);ctx.lineTo(w-pad.r,y);ctx.stroke();ctx.fillText(fmt(max*(1-i/4),0),2,y+3)}
  for(let hidx=0;hidx<24;hidx+=3){const x=pad.l+iw*hidx/23;ctx.fillText(String(hidx),x-4,h-7)}
  series.forEach((s,si)=>{ctx.strokeStyle=s.color;ctx.lineWidth=2;ctx.beginPath();plan.forEach((p,i)=>{const x=pad.l+iw*i/23;const y=pad.t+ih*(1-(Number(p[s.key])||0)/max);i?ctx.lineTo(x,y):ctx.moveTo(x,y)});ctx.stroke();ctx.fillStyle=s.color;ctx.fillRect(pad.l+si*78,pad.t,10,3);ctx.fillStyle='#b9cae0';ctx.fillText(s.label,pad.l+14+si*78,pad.t+5)});
}

async function optimize(){
  const btn=$('optimizeBtn'), error=$('errorBox'); error.classList.add('hidden'); error.textContent='';
  const payload=collect();
  if(!payload.scenario_id) return showError('Scenario ID is required.');
  if(payload.operator_notes.length<1 || payload.operator_notes.length>3) return showError('Enter 1 to 3 operator notes.');
  btn.disabled=true; btn.innerHTML='<span class="loading"></span> Optimizing…';
  try{
    const r=await fetch('/optimize-energy',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
    const body=await r.json().catch(()=>({detail:'Server returned a non-JSON error.'}));
    if(!r.ok) throw new Error(typeof body.detail==='string'?body.detail:JSON.stringify(body));
    renderResult(body);
  }catch(e){showError(e.message || String(e));}finally{btn.disabled=false;btn.textContent='Optimize energy plan';}
}

function showError(msg){const box=$('errorBox');box.textContent=msg;box.classList.remove('hidden');box.scrollIntoView({behavior:'smooth',block:'center'});}

async function init(){
  createHourRows();
  ['note1','note2','note3'].forEach(id=>$(id).addEventListener('input',updateNoteCount));
  $('loadSampleBtn').addEventListener('click',()=>loadSample().catch(e=>showError(e.message)));
  $('optimizeBtn').addEventListener('click',optimize);
  $('downloadResultBtn').addEventListener('click',()=>lastResult&&downloadJson(`${lastResult.scenario_id}-result.json`,lastResult));
  $('exportInputBtn').addEventListener('click',()=>downloadJson(`${$('scenarioId').value||'gridwise'}-input.json`,collect()));
  $('jsonFile').addEventListener('change',async e=>{const file=e.target.files?.[0];if(!file)return;try{populate(JSON.parse(await file.text()));}catch(err){showError('Invalid JSON file: '+err.message)}e.target.value='';});
  window.addEventListener('resize',()=>lastResult&&drawChart(lastResult.hourly_plan));
  try{
    const cfg=await (await fetch('/ui-config')).json();
    $('modelChip').textContent=cfg.model || 'Gemini';
    $('apiChip').textContent=cfg.mode==='demo'?'Demo mode':(cfg.api_ready?`Gemini ready • ${cfg.configured_key_count||1} key${(cfg.configured_key_count||1)===1?'':'s'}`:'API key missing');
    $('apiChip').classList.add(cfg.mode==='demo'||cfg.api_ready?'good':'warn');
  }catch{}
  await loadSample();
}

init().catch(e=>showError(e.message));
