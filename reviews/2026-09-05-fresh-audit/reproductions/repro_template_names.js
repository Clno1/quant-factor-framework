// Execute only the extracted handler in an empty vm, with delete mocked out.
const fs = require('fs');
const vm = require('vm');
const cases = JSON.parse(fs.readFileSync('/private/tmp/quant_fresh_audit_20260905/repro_template_names.json','utf8'));
const results=[];
for(const item of cases){
  const sandbox={deleteStrategy:()=>{}};
  let error=null;
  try{vm.runInNewContext(item.onclick,sandbox,{timeout:100});}catch(exc){error=exc.name+': '+exc.message;}
  results.push({...item,error,auditMarker:sandbox.auditMarker??null});
}
fs.writeFileSync('/private/tmp/quant_fresh_audit_20260905/repro_template_names_result.json',JSON.stringify(results,null,2));
console.log(JSON.stringify(results,null,2));
if(!results[0].error?.startsWith('SyntaxError')||results[1].auditMarker!==1)process.exit(1);
