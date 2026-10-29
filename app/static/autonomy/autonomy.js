'use strict';
const $=id=>document.getElementById(id);let session=null;let catalog=[];let chart=null;let series=null;let kronosData=null;
const token=()=>sessionStorage.getItem('arena_token')||'';
function esc(v){return String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
function note(message,error=false){$('notice').textContent=message;$('notice').className=error?'notice error':'notice';}
async function api(path,options={}){const headers={'Accept':'application/json',...(options.headers||{})};if(token())headers.Authorization='Bearer '+token();if(options.body)headers['Content-Type']='application/json';const response=await fetch('/api/autonomy'+path,{...options,headers});let body={};try{body=await response.json();}catch(_){body={detail:'Non-JSON server response'};}if(!response.ok)throw new Error(body.detail||('HTTP '+response.status));return body;}
async function ensureToken(){if(!token()){const value=window.prompt('Local API bearer token (not your Upstox credential):');if(value)sessionStorage.setItem('arena_token',value);}return token();}
async function searchCatalog(){try{await ensureToken();const q=encodeURIComponent($('catalogQuery').value);const data=await api('/catalog?limit=100&query='+q);catalog=data.instruments;$('catalogMeta').textContent=`${data.total} exact instruments · refreshed ${data.refreshed_at||'never'} · ${data.scope}`+(function(){const s=data.stats||{};const kept=Object.entries(s.kept_by_segment||{}).map(([k,v])=>`${k} ${v}`).join(', ');const diag=[s.source_rows!==undefined?`BOD rows ${s.source_rows}`:'',kept?`kept ${kept}`:'',(s.dropped_suspended||s.dropped_unsupported||s.dropped_invalid)?`dropped: suspended ${s.dropped_suspended||0}, unsupported ${s.dropped_unsupported||0}, invalid ${s.dropped_invalid||0}`:'',...(s.dropped_invalid_samples||[]),...(data.warnings||[]).slice(0,3)].filter(Boolean);return diag.length?` · ${diag.join(' · ')}`:'';})();$('catalogRows').innerHTML=catalog.map(item=>`<tr><td><code>${esc(item.instrument_key)}</code><br>${esc(item.trading_symbol)}</td><td>${esc(item.product_label)}<br><small>tick ${esc(item.tick_size)} · step ${esc(item.quantity_step)} · ${item.risk_metadata_complete?'metadata complete':'metadata incomplete'}</small></td><td><button data-add="${esc(item.instrument_key)}">Select</button></td></tr>`).join('');document.querySelectorAll('[data-add]').forEach(button=>button.onclick=()=>{const lines=$('selectedKeys').value.split(/\n/).filter(Boolean);if(!lines.includes(button.dataset.add))lines.push(button.dataset.add);$('selectedKeys').value=lines.join('\n');});}catch(error){note(error.message,true);}}
async function loadSessions(ask=false){
  try{
    if(ask)await ensureToken();
    if(!token())return;
    const previous=$('existingSessions').value||sessionStorage.getItem('autonomy_session_id')||'';
    const data=await api('/status');
    $('existingSessions').innerHTML='<option value="">Select a recovered/loaded session</option>'+(data.sessions||[]).map(item=>`<option value="${esc(item.session_id)}">${esc(item.session_id)} · ${esc(item.mode)} · ${esc(item.state)}</option>`).join('');
    if(previous&&(data.sessions||[]).some(item=>item.session_id===previous)){
      $('existingSessions').value=previous;
      session=await api('/sessions/'+previous+'/recover-view',{method:'POST'});
      sessionStorage.setItem('autonomy_session_id',previous);
      render();
      note('Loaded durable read-only view. Prior authorization/risk/Arm/Activate approval was revoked.');
    }
  }catch(error){note(error.message,true);}
}
async function createSession(){try{await ensureToken();const instrument_keys=$('selectedKeys').value.split(/\n/).map(x=>x.trim()).filter(Boolean);session=await api('/sessions',{method:'POST',body:JSON.stringify({mode:$('mode').value,instrument_keys})});sessionStorage.setItem('autonomy_session_id',session.session_id);note('Session created UNCONFIGURED. No order or execution permit was created.');render();}catch(error){note(error.message,true);}}
async function action(name,body){
  if(!session)return note('Create/select a session first.',true);
  try{
    await ensureToken();
    session=await api('/sessions/'+session.session_id+'/'+name,{method:'POST',...(body?{body:JSON.stringify(body)}:{})});
    if(name==='emergency-stop'&&session.emergency?.cleanup_status!=='CONFIRMED')note('Emergency halted entries, but managed-order cleanup is UNRESOLVED. Account clear is not claimed.',true);
    else note(name+' completed; verify status, orders and audit.');
    render();
    if(name==='activate'||name==='scan')loadBars();
  }catch(error){note(error.message,true);await reload();}
}
async function reload(){if(!session)return;try{session=await api('/sessions/'+session.session_id);render();}catch(error){note(error.message,true);}}
function pill(text,kind=''){return `<span class="pill ${kind}">${esc(text)}</span>`;}
function render(){
  if(!session)return;
  $('sessionPanel').classList.remove('hidden');
  $('sessionId').textContent=session.session_id;
  const live=session.mode==='UPSTOX_LIVE';
  $('modeBanner').className='banner'+(live?' live':'');
  $('modeBanner').innerHTML=`<b>${live?'UPSTOX_LIVE · REAL-MONEY BOUNDARY':'PAPER · LOCAL SIMULATED FILLS'}</b> · ${esc(session.state)}${session.recovered?' · RECOVERED VIEW':''} · ${session.entries_blocked?'ENTRIES BLOCKED':'entries permitted only when ACTIVE'}<br><small>${esc(session.operator_note)}</small>`;
  $('authState').textContent=session.authorized_day||'Not authorized';
  $('authState').className='pill'+(session.authorized_day?' good':'');
  $('statusMetrics').innerHTML=[['State',session.state],['Mode',session.mode],['Scans',session.scan_count],['Last scan',session.last_scan_kind||'none'],['Working',session.working_instruments.length],['Kronos',session.kronos.status]].map(([a,b])=>`<span class="metric"><small>${a}</small><b>${esc(b)}</b></span>`).join('');
  $('fault').textContent=session.fault?`${session.fault}: ${session.fault_reason} Fresh authorization, risk, Arm + Activate approval is required.`:(session.fresh_approval_required?'Fresh operator approval required.':'');
  const emergency=session.emergency||{};
  if(emergency.cleanup_status){
    const unresolved=(emergency.unresolved_orders||[]).length;
    $('emergencyStatus').textContent=`EMERGENCY CLEANUP ${emergency.cleanup_status}: ${unresolved} unresolved managed order(s); open positions: ${(emergency.open_positions||[]).join(', ')||'none reported'}. Account clear is NOT claimed.`;
    $('emergencyStatus').className='notice'+(emergency.cleanup_status==='CONFIRMED'?'':' error');
  }else{
    $('emergencyStatus').textContent='';
    $('emergencyStatus').className='notice';
  }
  const r=session.readiness||{};
  $('readiness').innerHTML=`<p>${pill(r.ready?'Preflight structure ready':'Not ready',r.ready?'good':'warn')} ${esc((r.blockers||[]).join(' · '))}</p>`;
  const a=session.account;
  $('account').innerHTML=a?`<span class="metric"><small>Equity</small><b>${esc(a.equity)}</b></span><span class="metric"><small>Available margin</small><b>${esc(a.available_margin)}</b></span><span class="metric"><small>Day P&amp;L</small><b>${esc(a.day_pnl)}</b></span><span class="metric"><small>Source</small><b>${a.authoritative?'Upstox account':'Local simulation'}</b></span>`:'<span class="muted">No account snapshot yet.</span>';
  $('arm').disabled=session.state==='ACTIVE';
  $('activate').disabled=session.state!=='ARMED';
  $('pause').disabled=session.state!=='ACTIVE';
  $('scan').disabled=session.state!=='ACTIVE';
  $('cancelUnfilled').disabled=!(session.working_instruments||[]).length;
  $('positions').innerHTML=(a?.positions||[]).map(p=>`<tr><td><code>${esc(p.instrument_key)}</code></td><td>${esc(p.quantity)}</td><td>${esc(p.average_price)} / ${esc(p.last_price)}</td><td>${esc(p.stop_price)} / ${esc(p.target_price)}</td><td>${p.protection_id?pill('confirmed','good'):pill('UNCONFIRMED','bad')}</td></tr>`).join('')||'<tr><td colspan="5" class="muted">No open positions reported.</td></tr>';
  $('intents').innerHTML=(session.intents||[]).map(i=>`<tr><td><code>${esc(i.intent_id)}</code><br><small>${esc(i.state)}${i.recovered_view?' · last-known':''}</small></td><td><code>${esc(i.instrument_key)}</code></td><td>${esc(i.state)}</td><td><code>${esc((i.broker_ids||[]).join(', '))}</code></td></tr>`).join('')||'<tr><td colspan="4" class="muted">No durable intents.</td></tr>';
  $('orders').innerHTML=(session.orders||[]).map(o=>`<tr><td><code>${esc(o.gtt_order_id||'')}</code></td><td><code>${esc(o.instrument_token||'')}</code></td><td><code>${esc((o.rules||[]).map(rule=>`${rule.strategy}:${rule.status}@${rule.trigger_price}`).join(' · '))}</code></td></tr>`).join('')||'<tr><td colspan="3" class="muted">No last-known GTT records.</td></tr>';
  $('kronos').innerHTML=`<p>${pill('Kronos '+session.kronos.status,session.kronos.available_for_inference&&session.kronos.audit_status!=='DEGRADED'?'good':'warn')} <small>${esc((session.kronos.issues||[]).join(' · ')||session.kronos.fallback)}</small></p><small>Verified artifacts only; no mock forecast or Kronos attribution on fallback.</small>`;
  $('evidence').innerHTML=(session.evidence||[]).map(e=>`<tr><td>${esc(e.strategy_family)} / ${esc(e.side)}</td><td><code>${esc(e.instrument_key)}</code></td><td>${esc(e.trades)} trades · expectancy ${esc(e.net_expectancy)} · DD ${esc(e.max_drawdown)}</td><td>${pill(e.provenance_class||'unknown',e.live_eligible_structure?'warn':'bad')}<br><small>${esc(e.provenance_effect||'')}. ${esc(e.paper_execution_effect||'PAPER and LIVE execution blocked')}.</small></td></tr>`).join('')||'<tr><td colspan="4" class="muted">No research evidence installed; candidates remain non-executable.</td></tr>';
  $('diagnostics').innerHTML=(session.diagnostics||[]).slice(-40).reverse().map(d=>`<tr><td><code>${esc(d.instrument_key)}</code></td><td>${esc(d.family)} ${esc(d.side||'')}</td><td>${esc(d.explanation||d.rejected||d.status||'')}</td></tr>`).join('')||'<tr><td colspan="3" class="muted">No completed-bar diagnostics yet.</td></tr>';
  const select=$('chartInstrument');
  const old=select.value;
  select.innerHTML=session.instruments.map(i=>`<option value="${esc(i.instrument_key)}">${esc(i.display_symbol)}</option>`).join('');
  if(old)select.value=old;
}
async function loadAudit(){if(!session)return;try{const data=await api('/sessions/'+session.session_id+'/audit?limit=100');$('chain').textContent=data.hash_chain_valid?'hash chain valid':'HASH CHAIN INVALID';$('audit').innerHTML=data.events.map(e=>`<tr><td>${esc(e.event_type)}</td><td>${esc(e.ts)}</td><td><code>${esc(JSON.stringify(e.payload))}</code></td></tr>`).join('');}catch(error){note(error.message,true);}}
async function loadBars(){if(!session||!$('chartInstrument').value)return;try{const key=$('chartInstrument').value;const data=await api('/sessions/'+session.session_id+'/bars/'+encodeURIComponent(key));if(!chart){chart=LightweightCharts.createChart($('chart'),{layout:{background:{color:'#101c29'},textColor:'#edf4fb'},grid:{vertLines:{color:'#294055'},horzLines:{color:'#294055'}},timeScale:{timeVisible:true,secondsVisible:false}});series=chart.addSeries(LightweightCharts.CandlestickSeries,{upColor:'#66d9c2',downColor:'#ff8d8d',wickUpColor:'#66d9c2',wickDownColor:'#ff8d8d'});}series.setData(data.bars.map(b=>({time:Math.floor(new Date(b.timestamp).getTime()/1000),open:b.open,high:b.high,low:b.low,close:b.close})));chart.timeScale().fitContent();}catch(error){note(error.message,true);}}
function bytes(value){const n=Number(value||0);if(n>=1073741824)return (n/1073741824).toFixed(2)+' GiB';if(n>=1048576)return (n/1048576).toFixed(2)+' MiB';return Math.round(n/1024)+' KiB';}
function renderKronos(){
  if(!kronosData)return;
  const d=kronosData,plan=d.install_plan||{},op=d.operation||{};
  const available=d.available_for_inference===true;
  const healthy=available&&d.audit_status!=='DEGRADED';
  $('kronosSetupPill').textContent=healthy?'READY · REAL INFERENCE AVAILABLE':(d.status||'UNAVAILABLE');
  $('kronosSetupPill').className='pill '+(healthy?'good':op.state==='ERROR'?'bad':'warn');
  const component=name=>{
    const item=plan[name]||{};
    return `<h3>${esc(name)} · ${esc(item.repository||'')} @ <code>${esc(item.revision||'')}</code></h3><p><small>Expected ${bytes(item.expected_bytes)} · pinned HTTPS revision</small></p>`+(item.files||[]).map(file=>`<p><code>${esc(file.name)}</code><br><small>${bytes(file.size)} · SHA-256 ${esc(file.sha256)}<br>${esc(file.url)}</small></p>`).join('');
  };
  const runtime=plan.runtime||{};
  $('kronosInstallPlan').innerHTML=`<p><small>Source ${esc(plan.source?.repository||'')} @ <code>${esc(plan.source?.commit||'')}</code><br>Total model + tokenizer ${bytes(plan.expected_total_bytes)} · ${esc(plan.atomic_install||'')}<br>Manifest file SHA-256 <code>${esc(plan.manifest_file_sha256||'')}</code><br>${esc(plan.location||'')}</small></p>${component('model')}${component('tokenizer')}<h3>Locked CPU runtime</h3><p><small>${esc(runtime.python||'')} · ${(runtime.packages||[]).length} exact packages · no CUDA stack<br>Runtime lock <code>${esc(runtime.lock_sha256||'')}</code><br>Minimum free space ${bytes(runtime.minimum_free_bytes)} · ${esc((runtime.indexes||[]).join(' · '))}</small></p>${d.install_plan_error?`<p class="error">${esc(d.install_plan_error)}</p>`:''}`;
  const pct=op.total_bytes?Math.min(100,Math.round(100*Number(op.bytes||0)/Number(op.total_bytes))):0;
  $('kronosOperation').textContent=op.kind?`${op.kind} · ${op.state} · ${op.phase||''}${op.total_bytes?` · ${pct}% (${bytes(op.bytes)} / ${bytes(op.total_bytes)})`:''}${op.component?` · ${op.component}/${op.file||''}`:''}${op.error?` · ${op.error.message}`:''}`:'No setup operation running. Review the pinned sources, revisions, sizes and hashes above before installing.';
  const rt=d.runtime||{},last=d.last_self_test,py=rt.python||{};
  const pythonText=py.version?`${py.implementation||'CPython'} ${py.version.join('.')} ${py.machine||''} ${py.bits||''}-bit · pip ${py.pip||'unknown'} · ${py.command_label||py.selection||'automatic'}`:'No supported CPython 3.11/3.12 x64 interpreter with pip has been detected.';
  const present=name=>{const files=Object.values(d[name]?.files||{});return files.length>0&&files.every(file=>file.present);};
  const fileDetails=name=>Object.entries(d[name]?.files||{}).map(([file,view])=>esc(`${name}/${file}: ${view.present?'present':'missing'}, ${view.verified?'SHA-256 verified':view.issue||'unverified'}`)).join('<br>');
  $('kronosSetupStatus').innerHTML=`<p><small>Source: ${present('source')?'present':'incomplete'} / ${esc(d.source?.status||'NOT_CHECKED')} · Model: ${present('model')?'present':'incomplete'} / ${esc(d.model?.status||'NOT_CHECKED')} · Tokenizer: ${present('tokenizer')?'present':'incomplete'} / ${esc(d.tokenizer?.status||'NOT_CHECKED')}<br>Runtime: ${rt.installed?'installed':'not installed'} / ${esc(rt.status||d.inference_runtime||'NOT_CHECKED')}<br>${esc(pythonText)}<br>${fileDetails('model')}<br>${fileDetails('tokenizer')}<br>Loaded now: ${d.loaded?'YES · offline worker active':'no persistent model in the server'} · Real forecast: ${d.last_inference?.status||'not yet produced'}<br>${esc((d.issues||[]).join(' · ')||'No current setup issue.')}</small></p>${last?`<p><small>Last self-test ${esc(last.status)} · ${esc(last.input_label)}<br>Device ${esc(last.device)} · load ${esc(Math.round(Number(last.load_ms||0)))} ms · inference ${esc(Math.round(Number(last.inference_ms||0)))} ms<br>Artifact ${esc(last.manifest_sha256)}<br>Runtime ${esc(last.runtime_lock_sha256)}</small></p>`:'<p><small>No successful real-inference self-test is recorded for this exact artifact/runtime identity.</small></p>'}`;
  $('installKronos').disabled=op.active===true;
  $('installKronos').textContent=op.state==='ERROR'?'Retry pinned Kronos setup':'Download & install pinned Kronos';
  $('cancelKronos').disabled=op.active!==true;
  $('testKronos').disabled=op.active===true||!available;
  $('selectKronosPython').disabled=op.active===true;
  $('autoKronosPython').disabled=op.active===true;
  $('kronosPythonExecutable').disabled=op.active===true;
}
async function loadKronos(){if(!token())return;try{kronosData=await api('/kronos');renderKronos();}catch(error){note(error.message,true);}}
async function kronosPythonAction(automatic){
  try{await ensureToken();const executable=automatic?'':$('kronosPythonExecutable').value.trim();if(!automatic&&!executable)throw new Error('Enter one full Python executable path, or choose Auto-detect Python.');kronosData=await api('/kronos/python',{method:'POST',body:JSON.stringify({executable})});$('kronosPythonExecutable').value='';renderKronos();const py=kronosData.runtime?.python;note(py?.version?`${py.implementation||'CPython'} ${py.version.join('.')} x64 with pip ${py.pip} validated for Kronos only. No trading or execution state changed.`:'Automatic Python discovery completed; no supported interpreter was found. No trading or execution state changed.',!py?.version);}catch(error){note(error.message,true);}
}
async function kronosAction(name,confirmText){
  try{await ensureToken();if(!kronosData){await loadKronos();if(!kronosData)throw new Error('Kronos install plan is unavailable');}if(confirmText===true){const p=kronosData.install_plan||{};if(!window.confirm(`Download only the displayed pinned model/tokenizer (${bytes(p.expected_total_bytes)}) and locked CPU runtime? No weights or runtime go into the application directory. No GPU/CUDA stack or trading action is included.`))return;}kronosData=await api('/kronos/'+name,{method:'POST'});renderKronos();note(name==='cancel'?'Cancellation requested; partial setup files are removed.':'Kronos '+name+' started. No trading session or execution state changed.');}catch(error){note(error.message,true);}
}
$('selectKronosPython').onclick=()=>kronosPythonAction(false);
$('autoKronosPython').onclick=()=>kronosPythonAction(true);
$('installKronos').onclick=()=>kronosAction('install',true);
$('testKronos').onclick=()=>kronosAction('self-test','');
$('cancelKronos').onclick=()=>kronosAction('cancel','');
$('searchCatalog').onclick=searchCatalog;
$('refreshCatalog').onclick=async()=>{try{await ensureToken();await api('/catalog/refresh',{method:'POST'});note('BOD metadata refreshed. No session or execution state changed.');searchCatalog();}catch(error){note(error.message,true);}};
$('loadSession').onclick=()=>loadSessions(true);
$('createSession').onclick=createSession;
$('authorize').onclick=()=>action('authorize');
$('saveRisk').onclick=()=>action('risk',{max_daily_loss:Number($('maxLoss').value),per_trade_risk_fraction:Number($('riskFraction').value),...( $('mode').value==='PAPER'?{equity_at_entry:Number($('equity').value)}:{})});
$('arm').onclick=()=>action('arm');
$('activate').onclick=()=>{if(session?.mode==='UPSTOX_LIVE'&&!window.confirm('Activate real-money autonomous orders for this daily session? No per-order confirmation will follow.'))return;action('activate');};
$('pause').onclick=()=>action('pause');
$('emergency').onclick=()=>{if(window.confirm('Immediately block entries and attempt managed unfilled-entry cancellation? Open-position protection is preserved; unresolved orders will remain prominent.'))action('emergency-stop');};
$('scan').onclick=()=>action('scan');
$('reconcile').onclick=()=>action('reconcile');
$('cancelUnfilled').onclick=()=>{const key=$('chartInstrument').value;if(key&&window.confirm('Cancel only the selected unfilled entry after fresh reconciliation? Open-position protection will not be cancelled.'))action('cancel-unfilled',{instrument_key:key});};
$('loadAudit').onclick=loadAudit;
$('chartInstrument').onchange=loadBars;
if(token()){loadSessions();loadKronos();}
setInterval(loadKronos,3000);
setInterval(async()=>{if(session){await reload();await loadBars();}},7000);
