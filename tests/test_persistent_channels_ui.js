const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {test} = require('node:test');

// Execute the actual page script; avoid its startup requests in the fixture.
const template = fs.readFileSync(path.join(__dirname, '../templates/persistent_channels.html'), 'utf8');
const script = template.split('<script>')[1].split('</script>')[0]
  .split('\nloadPersistentFavoriteTeams().then(loadPersistentChannels);')[0];
function page() {
  const elements = new Map();
  const document = {
    getElementById(id) {
      if (!elements.has(id)) elements.set(id, {
        innerHTML:'', textContent:'', value:'', dataset:{}, hidden:false,
        setAttribute() {}, classList:{toggle() {}}, scrollIntoView() {},
      });
      return elements.get(id);
    },
  };
  const context = vm.createContext({document, URLSearchParams, setTimeout() {}, clearTimeout() {},
    window:{addEventListener() {}}, showToast() {}});
  vm.runInContext(script, context);
  vm.runInContext(`
    const fixtureChannel = {id:7, stream_id:'original', category_id:'sports', display_name:'Original', channel_number:'7', enabled:false};
    _persistentChannels = [fixtureChannel];
    _persistentResults = [{stream_id:'original', category_id:'sports', name:'Original'}];
    document.getElementById('persistent-search').value = 'sports';
    persistentRequest = async (url, options) => {
      if (options) { capturedSave = JSON.parse(options.body); return {channel:fixtureChannel}; }
      if (url.includes('/quality/queue')) return {requests:[], automatic:[]};
      if (url.includes('/search?')) return {items:[{stream_id:'different', category_id:'other', name:'Different'}], page:1, total:1};
      return {channels:[fixtureChannel]};
    };
  `, context);
  return {context, document, run:code => vm.runInContext(code, context)};
}

for (const mode of ['edit', 'add']) {
  test(`${mode} draft and EPG selection survive queue polling and browser changes`, async () => {
    const p = page();
    p.run(mode === 'edit' ? 'showPersistentEdit(7)' : 'showPersistentAdd(0)');
    const editor = p.document.getElementById('persistent-editor');
    const form = editor.innerHTML;
    const name = p.document.getElementById('persistent-form-name');
    name.value = 'Unsaved name';
    const source = p.document.getElementById('persistent-form-epg-source');
    source.value = 'xmltv:chosen';
    const links = p.document.getElementById('persistent-form-epg-links');
    links.innerHTML = 'Selected station and schedule';
    await p.run('loadQualityQueue()');
    p.run('clearPersistentBrowserResults(); setPersistentCategoryView("active"); closePersistentBrowser();');
    assert.equal(editor.innerHTML, form);
    assert.equal(name.value, 'Unsaved name');
    assert.equal(source.value, 'xmltv:chosen');
    assert.equal(links.innerHTML, 'Selected station and schedule');
  });
}

test('add draft saves the selected stream after search results reorder or disappear', async () => {
  const p = page();
  p.run('showPersistentAdd(0)');
  await p.run('loadQualityQueue()');
  assert.equal(p.run('_persistentResults[0].stream_id'), 'different');
  p.run('clearPersistentBrowserResults(); readPersistentForm = () => ({display_name:"Unsaved name"});');
  await p.run('savePersistentForm("add", null)');
  assert.equal(p.run('capturedSave.stream_id'), 'original');
  assert.equal(p.run('capturedSave.category_id'), 'sports');
  assert.equal(p.run('capturedSave.display_name'), 'Unsaved name');
});
