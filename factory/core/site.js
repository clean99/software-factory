(function(){
  var root=document.documentElement,order=['auto','light','dark'],mode='auto';
  try{mode=localStorage.getItem('theme')||'auto'}catch(e){}
  if(order.indexOf(mode)<0)mode='auto';
  function paint(){if(mode==='auto')root.removeAttribute('data-theme');else root.setAttribute('data-theme',mode)}
  paint();
  document.addEventListener('DOMContentLoaded',function(){
    var button=document.getElementById('theme');
    function label(){
      button.textContent=mode.charAt(0).toUpperCase()+mode.slice(1);
      button.setAttribute('aria-label','Colour theme: '+(mode==='auto'?'follows system':mode)+'. Activate to change.');
    }
    if(button){
      button.addEventListener('click',function(){mode=order[(order.indexOf(mode)+1)%3];try{localStorage.setItem('theme',mode)}catch(e){}paint();label()});
      label();
    }
    var stamp=document.getElementById('generated'),note=document.getElementById('stale');
    var age=stamp?(Date.now()-Date.parse(stamp.getAttribute('datetime')))/864e5:0;
    if(note&&age>2){
      note.textContent='This snapshot is '+Math.floor(age)+' days old. The scheduled refresh has not published since '+stamp.textContent+'; every number on this page is as of that date.';
      note.className='stale on';
    }
    Array.prototype.forEach.call(document.querySelectorAll('[data-tip]'),function(mark){
      var chart=mark.closest('.chart'),tip=chart&&chart.querySelector('.tooltip');
      if(!tip)return;
      function show(){tip.textContent=mark.getAttribute('data-tip');tip.hidden=false}
      function hide(){tip.hidden=true}
      mark.addEventListener('mouseenter',show);mark.addEventListener('focus',show);
      mark.addEventListener('mouseleave',hide);mark.addEventListener('blur',hide);
    });
  });
})();
