"use strict";
window.LifeUI=(()=>{
  const form=$('life-form');let dirty=false,current=null,loading=null;
  const make=(tag,text,cls)=>{const node=document.createElement(tag);node.textContent=text;if(cls)node.className=cls;return node;};
  function saved(){dirty=false;form.dataset.dirty='false';$('life-dirty').textContent='已保存';}
  function sync(settings,revision){
    if(!settings)return;
    current={settings,revision};
    if(dirty){if(Number(form.dataset.revision)!==revision)$('life-dirty').textContent='草稿保留 · 设置版本已变化';return;}
    form.dataset.revision=revision;
    for(const [key,value] of Object.entries(settings)){const field=form.elements.namedItem(key);if(!field)continue;if(field.type==='checkbox')field.checked=value;else field.value=value;}
  }
  for(const event of ['input','change'])form.addEventListener(event,()=>{dirty=true;form.dataset.dirty='true';$('life-dirty').textContent='草稿 · 未保存';});
  form.onsubmit=action(async()=>{if(!current)throw new Error('请等待设置加载');const settings=structuredClone(current.settings);for(const key of Object.keys(settings)){const field=form.elements.namedItem(key);settings[key]=field.type==='checkbox'?field.checked:field.type==='number'?Number(field.value):field.value;}await save({life:settings},Number(form.dataset.revision));await load();});
  async function load(){
    if(loading)return loading;
    loading=(async()=>{
      const data=await api('life');sync(data.settings,data.revision);
      const s=data.state;
      $('life-state').textContent=`${data.offline?'离线验证 · ':''}${s.mood} · 愉快 ${s.pleasure.toFixed(2)} / 活力 ${s.energy.toFixed(2)} / 紧张 ${s.tension.toFixed(2)}${s.interests.length?' · 最近喜欢：'+s.interests.join('、'):''} · 下次自主思考：${s.next_at?new Date(s.next_at*1000).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai'}):'待调度'}`;
      const box=$('life-stickers');box.replaceChildren();$('life-library-count').textContent=`${data.stickers.length} / ${data.settings.sticker_capacity} 张`;
      if(!data.stickers.length)box.append(make('p','还没有收藏。可以先上传几张喜欢的表情。','muted'));
      data.stickers.forEach(item=>{const card=make('article','','tool-card');const image=document.createElement('img');image.src='/api/life/image/'+encodeURIComponent(item.id);image.alt=item.description||'收藏的表情';image.loading='lazy';image.width=160;image.height=140;image.style.objectFit='contain';image.style.maxWidth='100%';card.append(image,make('p',item.description||'尚未描述'),make('small',({'chat':'聊天中喜欢的','upload':'管理员上传','search':'网上找到的'}[item.source]||item.source)));const button=make('button','移出本地表情库','text-button');button.type='button';button.onclick=()=>window.Operations.confirm('移出这个表情？','删除本地收藏；QQ 客户端已同步的收藏保留。',async()=>{await api('life',{action:'delete-sticker',id:item.id,revision:current.revision});await load();});card.append(button);box.append(card);});
      const actions=$('life-actions');actions.replaceChildren();if(!data.actions.length)actions.append(make('p','还没有自主操作记录。','muted'));
      data.actions.forEach(item=>{const row=make('article','','record-row');row.append(make('span',`${item.name} · ${item.chat==='life'?'自主时间':item.chat} · ${new Date(item.time*1000).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai'})}`),make('span',({confirmed:'已确认',pending:'提交中',unknown:'结果不明，不重试'}[item.status]||item.status),'pill '+(item.status==='confirmed'?'online':'attention')));actions.append(row);});
    })();try{await loading;}finally{loading=null;}
  }
  $('life-refresh').onclick=action(load);
  $('life-reload').onclick=()=>window.Operations.confirm('重新载入自主设置？','将放弃未保存的修改。',async()=>{saved();await load();});
  $('life-upload').onsubmit=action(async()=>{if(!current)throw new Error('请先刷新自主生活状态');const file=$('life-file').files[0];if(!file||file.size>6*1024*1024)throw new Error('请选择不超过6 MiB的图片');const encoded=await new Promise((resolve,reject)=>{const reader=new FileReader();reader.onload=()=>resolve(reader.result.split(',')[1]);reader.onerror=()=>reject(new Error('图片读取失败'));reader.readAsDataURL(file);});await api('life',{action:'upload',revision:current.revision,base64:encoded,description:$('life-description').value});$('life-upload').reset();await load();toast('已加入表情库');});
  return {sync,load,saved};
})();
