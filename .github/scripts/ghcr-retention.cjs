// Inspect every tagged manifest before deleting untagged platform/attestation candidates.
const {execFileSync} = require('node:child_process');
const manifestTypes = new Set([
  'application/vnd.oci.image.manifest.v1+json',
  'application/vnd.docker.distribution.manifest.v2+json',
]);
const configTypes = new Set([
  'application/vnd.oci.image.config.v1+json',
  'application/vnd.docker.container.image.v1+json',
]);
const layerTypes = new Set([
  'application/vnd.oci.image.layer.v1.tar',
  'application/vnd.oci.image.layer.v1.tar+gzip',
  'application/vnd.oci.image.layer.v1.tar+zstd',
  'application/vnd.oci.image.layer.nondistributable.v1.tar',
  'application/vnd.oci.image.layer.nondistributable.v1.tar+gzip',
  'application/vnd.oci.image.layer.nondistributable.v1.tar+zstd',
  'application/vnd.docker.image.rootfs.diff.tar.gzip',
  'application/vnd.docker.image.rootfs.foreign.diff.tar.gzip',
]);
function descriptorIsKnown(descriptor, types) {
  return descriptor && types.has(descriptor.mediaType) &&
    /^sha256:[a-f0-9]{64}$/.test(descriptor.digest) &&
    Number.isSafeInteger(descriptor.size) && descriptor.size >= 0;
}
function isRunnableManifest(manifest) {
  return manifest && manifest.schemaVersion === 2 &&
    manifestTypes.has(manifest.mediaType) &&
    !Object.hasOwn(manifest, 'manifests') &&
    !Object.hasOwn(manifest, 'subject') &&
    !Object.hasOwn(manifest, 'artifactType') &&
    descriptorIsKnown(manifest.config, configTypes) &&
    Array.isArray(manifest.layers) &&
    manifest.layers.every(layer => descriptorIsKnown(layer, layerTypes));
}
module.exports = async ({github, context, core}) => {
  const owner = context.repo.owner;
  const pkg = process.env.PACKAGE_NAME;
  const {data: account} = await github.rest.users.getByUsername({username: owner});
  const prefix = account.type === 'Organization' ? 'orgs' : 'users';
  const route = 'GET /' + prefix + '/' + encodeURIComponent(owner) +
    '/packages/container/' + encodeURIComponent(pkg) + '/versions';
  const versions = await github.paginate(route, {per_page: 100});
  const tagged = versions.filter(v => v.metadata?.container?.tags?.length)
    .sort((a, b) => Date.parse(b.created_at) - Date.parse(a.created_at));
  for (const version of tagged) {
    if (!/^sha256:[a-f0-9]{64}$/.test(version.name)) {
      throw new Error('Unexpected package digest; cleanup stopped.');
    }
    const ref = 'ghcr.io/' + owner.toLowerCase() + '/' + pkg + '@' + version.name;
    const manifest = JSON.parse(execFileSync('docker',
      ['buildx', 'imagetools', 'inspect', '--raw', ref],
      {encoding: 'utf8', timeout: 60000, maxBuffer: 8 * 1024 * 1024}));
    // Legacy attestations have ordinary config/layers fields but in-toto layers.
    // Only known runnable image media types permit any destructive cleanup.
    if (!isRunnableManifest(manifest)) {
      throw new Error('An index, attestation, or unknown manifest exists; cleanup stopped.');
    }
  }
  const keep = new Set(tagged.slice(0, 3).map(v => v.id));
  for (const v of tagged) {
    if (v.metadata.container.tags.includes('latest')) keep.add(v.id);
  }
  // At most 100 deletions per run; subsequent runs drain any backlog.
  const expired = tagged.filter(v => !keep.has(v.id)).reverse().slice(0, 100);
  core.setOutput('tagged_ids', expired.map(v => v.id).join(','));
  core.setOutput('safe_single_arch', 'true');
  core.info('Manifest guard passed. Retaining at least three tagged versions and latest.');
};
