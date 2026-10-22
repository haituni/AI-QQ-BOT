"use strict";
window.ToolMenu=(()=>{
  let catalog=[], dirty=false, current=null, loading=null, renderedSignature='';
  const mappings={daily_limit:'tools-daily-limit',max_calls:'tools-max-calls',max_rounds:'tools-max-rounds',timeout_seconds:'tools-timeout',result_chars:'tools-result-chars',search_results:'tools-search-results'};
  const icons={web_search:'⌕',web_read:'↗',calculator:'∑',current_time:'◷'};
  const el=(tag,text,cls)=>{const item=document.createElement(tag);item.textContent=text;if(cls)item.className=cls;return item;};
  function mark(){dirty=true;$('tools-dirty').textContent='草稿 · 未保存';$('tools-form').dataset.dirty='true';}
  function saved(){dirty=false;$('tools-form').dataset.dirty='false';$('tools-dirty').textContent='已保存';}
  function sync(settings,revision){
    if(!settings)return;current={settings,revision};
    if(dirty){if(Number($('tools-form').dataset.revision)!==revision)$('tools-dirty').textContent='草稿保留 · 版本已变化，请重新载入';return;}
    $('tools-form').dataset.revision=revision;
    const signature=JSON.stringify(settings);
    if(signature===renderedSignature&&catalog.length&&$('tool-catalog').children.length===catalog.length)return;
    renderedSignature=signature;
    $('tools-enabled').checked=settings.enabled;$('tools-proactive').checked=settings.allow_proactive;
    Object.entries(mappings).forEach(([key,id])=>$(id).value=settings[key]);
    $('tools-allowed-domains').value=settings.allowed_domains.join('\n');$('tools-blocked-domains').value=settings.blocked_domains.join('\n');
    renderCatalog(settings);
  }
  function renderCatalog(settings){
    const box=$('tool-catalog');box.replaceChildren();
    catalog.forEach(tool=>{const card=el('article','','tool-card');const top=el('div','','tool-card-top');const icon=el('span',icons[tool.name]||'◇','tool-symbol');const label=el('label','','checkbox-label');const check=document.createElement('input');check.type='checkbox';check.dataset.toolName=tool.name;check.checked=settings.enabled_tools.includes(tool.name);check.setAttribute('aria-label','允许'+tool.title);label.append(check,el('span','允许使用'));top.append(icon,label);card.append(top,el('h2',tool.title),el('p',tool.description),el('small',tool.name,'tool-function'),el('span',tool.group==='QQ 自主生活'?'QQ 生活 · 实际权限逐次检查':tool.network?'DeepSeek 联网 · 按接口规则计费':'本地执行 · 无工具接口费用','pill '+(tool.network?'':'online')));box.append(card);});
  }
  function read(){
    const settings=structuredClone(current.settings);settings.enabled=$('tools-enabled').checked;settings.allow_proactive=$('tools-proactive').checked;
    settings.enabled_tools=[...document.querySelectorAll('[data-tool-name]:checked')].map(input=>input.dataset.toolName);
    for(const [key,id] of Object.entries(mappings))settings[key]=Number($(id).value);
    settings.allowed_domains=$('tools-allowed-domains').value.split(/[\s,，]+/).filter(Boolean);settings.blocked_domains=$('tools-blocked-domains').value.split(/[\s,，]+/).filter(Boolean);
    return settings;
  }
  for(const event of ['input','change'])$('tools-form').addEventListener(event,mark);
  $('tools-form').onsubmit=action(async()=>{if(!catalog.length)throw new Error('工具菜单尚未加载，请稍后重试');await save({tools:read()},Number($('tools-form').dataset.revision));await load();});
  $('tools-reload').onclick=()=>window.Operations.confirm('重新载入工具设置？','未保存的工具修改会被放弃，读取最新权限。',async()=>{saved();await refresh();await load();});
  function testField(){const tool=catalog.find(t=>t.name===$('tool-test-name').value);if(!tool)return;const values={web_search:['搜索词','例如：DeepSeek 最新官方公告'],web_read:['公开网页链接','https://example.com/article'],calculator:['算式','(128*3+56)/4'],current_time:['IANA 时区','Asia/Shanghai']};const [label,placeholder]=values[tool.name]||['参数（JSON）','QQ生活工具请在真实聊天中由模型调用'];$('tool-test-label').textContent=label;$('tool-test-input').placeholder=placeholder;$('tool-test-input').value=tool.name==='current_time'?'Asia/Shanghai':'';$('tool-test-submit').textContent=tool.network?'运行联网工具 · 可能计费':'运行本地工具';}
  $('tool-test-name').onchange=testField;
  async function load(){if(loading)return loading;loading=(async()=>{const data=await api('tools');const previous=$('tool-test-name').value;const first=!catalog.length;catalog=data.catalog;
    $('tools-today').textContent=data.today_calls;$('tools-active').textContent=data.active;$('tools-budget').textContent='每日上限 '+data.settings.daily_limit+' 次';$('tool-status-pill').textContent=data.offline?'离线验证环境':data.settings.enabled?'工具权限已开启':'全部工具已暂停';
    if(!dirty)sync(data.settings,data.revision);
    const select=$('tool-test-name');const signature=JSON.stringify([data.settings.enabled,data.settings.enabled_tools,data.offline]);
    if(select.dataset.signature!==signature||!select.options.length){select.dataset.signature=signature;select.replaceChildren();catalog.forEach(tool=>{const option=new Option(tool.title+(tool.network?' · 联网':' · 本地'),tool.name);option.disabled=tool.group==='QQ 自主生活'||!data.settings.enabled||!data.settings.enabled_tools.includes(tool.name)||(data.offline&&tool.network);select.add(option);});
      const enabled=[...select.options].filter(option=>!option.disabled);select.value=enabled.some(option=>option.value===previous)?previous:(enabled.find(option=>option.value==='calculator')||enabled[0])?.value||'';
      $('tool-test-submit').disabled=!select.value;
      if(first||select.value!==previous)testField();
    }
    const runs=$('tool-runs');runs.replaceChildren();if(!data.runs.length)runs.append(el('p','还没有工具执行记录。可以先试运行本地计算器或时间查询。','empty'));
    data.runs.forEach(run=>{const row=el('article','','record-row');const text=el('div','');text.append(el('strong',catalog.find(tool=>tool.name===run.name)?.title||run.name),el('small',(state?.config.conversations[run.chat]?.name||({'tool-test':'管理端试运行'}[run.chat])||run.chat)+' · '+new Date(run.time*1000).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai'})+' · '+run.elapsed.toFixed(2)+' 秒'));if(run.detail)text.append(el('small',run.detail));row.append(text,el('span',{running:'执行中',success:'已完成',error:'未完成',interrupted:'已中断'}[run.status]||run.status,'pill '+(run.status==='success'?'online':'attention')));runs.append(row);});
  })();try{await loading;}finally{loading=null;}}
  $('tools-refresh').onclick=action(()=>load());
  $('tool-test-form').onsubmit=action(async()=>{if(dirty)throw new Error('请先保存工具设置，再试运行');const name=$('tool-test-name').value,tool=catalog.find(t=>t.name===name);if(!tool)throw new Error('请先启用一个工具');const key=Object.keys(tool.parameters)[0];$('tool-test-result').textContent='工具正在执行…';try{const result=await api('tools/test',{revision:current.revision,name,arguments:{[key]:$('tool-test-input').value.trim()}});$('tool-test-result').textContent=JSON.stringify(result,null,2);toast(result.ok?'工具执行完成':result.error,!result.ok);}catch(error){$('tool-test-result').textContent=error.message;throw error;}finally{await load();}});
  return {sync,load,saved};
})();
