document.getElementById('login-form').addEventListener('submit',async event=>{
  event.preventDefault();const button=document.getElementById('submit');const result=document.getElementById('result');
  button.disabled=true;button.textContent='正在登录…';result.textContent='';result.className='';
  try{
    const response=await fetch('/auth/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({password:document.getElementById('password').value})});
    const data=await response.json();if(!response.ok)throw new Error(data.detail || '无法登录，请稍后重试。');
    document.getElementById('password').value='';location.replace('/');
  }catch(error){result.textContent=error.message;result.className='error';}
  finally{button.disabled=false;button.textContent='登录';}
});
