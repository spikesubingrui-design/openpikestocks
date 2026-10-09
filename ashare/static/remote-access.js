let setupToken;
const status = document.getElementById('connection');
const result = document.getElementById('result');
const submit = document.getElementById('submit');
async function refresh(){
  const response = await fetch('/api/remote/status',{cache:'no-store'});
  if(!response.ok) throw new Error('无法读取配置，请返回看板检查主机服务。');
  const data = await response.json();setupToken = data.setup_token;
  status.textContent = data.cloud_status === 'VERIFIED' && data.bridge_connected ? '远程入口已接通，主机转发在线' : data.cloud_status === 'VERIFIED' ? '远程入口已配置，主机转发暂时离线' : '已准备子域名，云端转发与证书尚待接通';
  document.getElementById('password-state').textContent = data.password_configured ? '已设置密码。保存新密码会退出其他设备。' : '尚未设置密码；远程访问暂不允许登录。';
}
refresh().catch(error=>{status.textContent=error.message;submit.disabled=true;});
document.getElementById('password-form').addEventListener('submit',async event=>{
  event.preventDefault();result.className='';result.textContent='';
  const password=document.getElementById('password').value;
  if(password!==document.getElementById('confirm').value){result.className='error';result.textContent='两次密码不一致，请检查。';return;}
  submit.disabled=true;submit.textContent='正在保存…';
  try{
    const response=await fetch('/api/remote/password',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({password,setup_token:setupToken})});
    const data=await response.json();if(!response.ok) throw new Error(data.detail || '保存失败，请重新打开设置页。');
    document.getElementById('password-form').reset();result.textContent='密码已保存。云端入口接通后即可在其他设备登录。';await refresh();
  }catch(error){result.className='error';result.textContent=error.message;}
  finally{submit.disabled=false;submit.textContent='保存密码';}
});
