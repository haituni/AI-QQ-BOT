"use strict";
window.QQLogin=(()=>{
  let timer=null, epoch=0, busy=false;
  const dialog=$('qq-login-dialog');
  function stop(){clearTimeout(timer);timer=null;epoch++;busy=false;}
  function render(data){
    const state=data.state;$('qq-login-message').textContent=data.message;
    $('qq-login-image').hidden=state!=='qrcode';$('qq-login-placeholder').hidden=state==='qrcode';
    if(state==='qrcode'){if($('qq-login-image').getAttribute('src')!==data.image)$('qq-login-image').src=data.image;}else $('qq-login-image').removeAttribute('src');
    const pending=['starting','preparing','scanned','connecting'].includes(state);
    $('qq-login-placeholder').replaceChildren();
    const mark=document.createElement('span');mark.className=pending?'qq-login-spinner':'qq-login-mark';mark.textContent=pending?'':state==='online'?'✓':'!';mark.setAttribute('aria-hidden','true');const label=document.createElement('p');label.textContent={starting:'启动 QQ 接入中',preparing:'正在获取二维码',scanned:'请在手机上确认',connecting:'正在连接消息通道',online:'QQ 已登录',needs_account:'先设置机器人账号',two_factor:'需要动态验证',failed:'启动未完成',unavailable:'接入尚未就绪',error:'暂时无法登录'}[state]||'请稍候';$('qq-login-placeholder').append(mark,label);
    $('qq-login-refresh').hidden=!['qrcode','scanned'].includes(state);$('qq-login-retry').hidden=!['failed','unavailable','error'].includes(state);$('qq-login-settings').hidden=state!=='needs_account';$('qq-login-2fa').hidden=state!=='two_factor';$('qq-login-done').hidden=state!=='online';
    $('qq-login-hint').textContent=data.hint||(state==='starting'?'首次安装可能需要下载文件。你可以留在此处等待，也可以关闭窗口稍后再试。':'无需输入接入密钥，扫码后在手机 QQ 确认即可。');
    $('qq-login-progress').hidden=!data.job?.lines?.length;if(data.job?.lines)$('qq-login-progress').textContent=data.job.lines.join('\n');
    return pending||state==='qrcode';
  }
  async function request(action='poll',totp=''){
    if(busy||!dialog.open)return;busy=true;const token=epoch;$('qq-login-refresh').disabled=$('qq-login-retry').disabled=true;
    try{const data=await api('qq-login',{action,...(totp?{totp}:{})});if(token!==epoch||!dialog.open)return;const again=render(data);if(data.state==='online')await refresh(false);if(again)timer=setTimeout(()=>request(),2500);}
    catch(error){if(token===epoch&&dialog.open)render({state:'error',message:error.message});}
    finally{if(token===epoch){busy=false;$('qq-login-refresh').disabled=$('qq-login-retry').disabled=false;}}
  }
  function open(){stop();busy=false;dialog.showModal();$('qq-login-account').textContent=state?.config.bot_qq?'请使用机器人 QQ '+state.config.bot_qq+' 扫码':'请先设置专用的机器人 QQ 账号';render({state:'preparing',message:'正在自动认证 QQ 接入…'});request('open');}
  $('qq-login-close').onclick=()=>dialog.close();dialog.addEventListener('close',()=>{stop();$('qq-login-image').removeAttribute('src');$('qq-login-totp').value='';});
  $('qq-login-refresh').onclick=action(async()=>{stop();await request('refresh');});$('qq-login-retry').onclick=action(async()=>{stop();await request('open');});$('qq-login-done').onclick=()=>dialog.close();$('qq-login-settings').onclick=()=>{dialog.close();page('settings');$('bot-qq').focus();};
  $('qq-login-2fa').onsubmit=action(async()=>{const code=$('qq-login-totp').value.trim();$('qq-login-totp').value='';stop();await request('open',code);});
  return {open};
})();
