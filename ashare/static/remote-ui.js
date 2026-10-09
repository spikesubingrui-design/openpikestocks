const originalFetch=window.fetch.bind(window);
window.fetch=async (...args)=>{
  const response=await originalFetch(...args);
  if(response.status===401)location.replace('/login');
  return response;
};
document.getElementById('remote-logout')?.addEventListener('click',async ()=>{
  const response=await originalFetch('/auth/logout',{method:'POST'});
  if(response.ok)location.replace('/login');
});
