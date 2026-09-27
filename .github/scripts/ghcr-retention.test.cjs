const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, 'ghcr-retention.cjs'), 'utf8');
const descriptor = mediaType => ({mediaType, digest:'sha256:'+'a'.repeat(64), size:123});
const ociImage = () => ({
  schemaVersion:2, mediaType:'application/vnd.oci.image.manifest.v1+json',
  config:descriptor('application/vnd.oci.image.config.v1+json'),
  layers:[descriptor('application/vnd.oci.image.layer.v1.tar+gzip')],
});
function version(id, tags) {
  return {id, name:'sha256:'+String(id).padStart(64,'0'),
    created_at:new Date(2026,0,id).toISOString(), metadata:{container:{tags}}};
}
async function run(versions, manifest=ociImage(), failure=false, accountType='User') {
  const outputs={}, calls=[], module={exports:{}};
  vm.runInNewContext(source, {module, process:{env:{PACKAGE_NAME:'project/api'}},
    require(name) {
      assert.equal(name,'node:child_process');
      return {execFileSync(command,args,options) {
        calls.push({command,args,options});
        if (failure) throw Error('registry unavailable');
        return JSON.stringify(typeof manifest==='function' ? manifest(calls.length) : manifest);
      }};
    }});
  const github={rest:{users:{getByUsername:async()=>({data:{type:accountType}})}},
    paginate:async route=>{
      const prefix=accountType==='Organization'?'orgs':'users';
      assert.equal(route,'GET /'+prefix+'/example/packages/container/project%2Fapi/versions');
      return versions;
    }};
  try {
    await module.exports({github,context:{repo:{owner:'example'}},
      core:{setOutput:(key,value)=>outputs[key]=value,info(){}}});
  } catch (error) {
    assert.deepEqual(outputs,{},'no deletion output may be emitted before all manifests pass');
    throw error;
  }
  return {outputs,calls};
}
test('keeps newest three tagged versions and older latest; bounds SHA history separately',async()=>{
  const versions=[version(1,['latest']),version(2,['sha-old']),version(3,['sha-a']),
    version(4,['sha-b']),version(5,['sha-c']),version(6,[])];
  const {outputs,calls}=await run(versions);
  assert.equal(outputs.tagged_ids,'2');
  assert.equal(outputs.safe_single_arch,'true');
  assert.equal(calls.length,5);
  assert.ok(calls.every(c=>c.command==='docker'&&c.args.includes('--raw')));
});
test('valid Docker schema-2 and OCI zstd images permit cleanup',async()=>{
  const docker={schemaVersion:2,mediaType:'application/vnd.docker.distribution.manifest.v2+json',
    config:descriptor('application/vnd.docker.container.image.v1+json'),
    layers:[descriptor('application/vnd.docker.image.rootfs.diff.tar.gzip')]};
  assert.equal((await run([version(1,['latest'])],docker)).outputs.safe_single_arch,'true');
  const zstd=ociImage(); zstd.layers=[descriptor('application/vnd.oci.image.layer.v1.tar+zstd')];
  assert.equal((await run([version(1,['latest'])],zstd)).outputs.safe_single_arch,'true');
});
test('OCI and Docker multi-platform indexes block all cleanup',async()=>{
  for(const mediaType of ['application/vnd.oci.image.index.v1+json','application/vnd.docker.distribution.manifest.list.v2+json'])
    await assert.rejects(run([version(1,['latest'])],{schemaVersion:2,mediaType,manifests:[{}]}),/cleanup stopped/);
});
test('valid zero-layer OCI images are retained without treating missing config as valid',async()=>{
  const scratch=ociImage(); scratch.layers=[];
  assert.equal((await run([version(1,['latest'])],scratch)).outputs.safe_single_arch,'true');
  await assert.rejects(run([version(1,['latest'])],{...scratch,config:{}}),/cleanup stopped/);
});
test('legacy in-toto attestations with normal image config block cleanup',async()=>{
  const legacy=ociImage(); legacy.layers=[descriptor('application/vnd.in-toto+json')];
  await assert.rejects(run([version(1,['latest'])],legacy),/cleanup stopped/);
});
test('OCI artifact and subject referrers block cleanup',async()=>{
  for(const extra of [{artifactType:'attestation'},{artifactType:''},{subject:{}},{subject:null}])
    await assert.rejects(run([version(1,['latest'])],{...ociImage(),...extra}),/cleanup stopped/);
});
test('unknown manifest/config/layer media types and malformed descriptors fail closed',async()=>{
  const invalid=[{}, {...ociImage(),mediaType:'application/unknown'},
    {...ociImage(),config:descriptor('application/vnd.oci.empty.v1+json')},
    {...ociImage(),layers:[descriptor('application/unknown')]},
    {...ociImage(),config:{...ociImage().config,size:-1}},
    {...ociImage(),layers:[{...ociImage().layers[0],digest:'unexpected'}]}];
  for(const manifest of invalid)
    await assert.rejects(run([version(1,['latest'])],manifest),/cleanup stopped/);
});
test('an older unsafe manifest blocks outputs after newer safe manifests passed',async()=>{
  const unsafe=ociImage(); unsafe.layers=[descriptor('application/vnd.in-toto+json')];
  await assert.rejects(run([version(1,['old']),version(2,['latest'])],index=>index===1?ociImage():unsafe),/cleanup stopped/);
});
test('unavailable registry and malformed package digests fail closed',async()=>{
  await assert.rejects(run([version(1,['latest'])],undefined,true),/registry unavailable/);
  await assert.rejects(run([{...version(1,['latest']),name:'unexpected'}]),/Unexpected package digest/);
});
test('empty registry schedules no tagged deletion',async()=>{
  assert.equal((await run([])).outputs.tagged_ids,'');
});
test('caps tagged deletion backlog at 100 while retaining three',async()=>{
  const {outputs}=await run(Array.from({length:110},(_,i)=>version(i+1,['sha-x'])));
  assert.equal(outputs.tagged_ids.split(',').length,100);
});
test('organization packages use the organization API path',async()=>{
  assert.equal((await run([version(1,['latest'])],ociImage(),false,'Organization')).outputs.safe_single_arch,'true');
});
