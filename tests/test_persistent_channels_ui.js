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

test('channel order mode puts the number input before the logo and saves an inline edit', async () => {
  const p = page();
  p.run(`_persistentChannels[0].logo = '/static/logo.png'; togglePersistentNumberMode();`);
  const html = p.document.getElementById('persistent-list').innerHTML;
  assert.ok(html.indexOf('class="persistent-channel-number"') < html.indexOf('<img src="/static/logo.png"'));
  assert.equal(p.document.getElementById('persistent-number-mode').textContent, 'Done editing order');
  p.run(`
    const inlineInput = {value:'8', parentElement:{querySelector(){ return {disabled:false,textContent:''}; }}};
    persistentRequest = async (url, options) => {
      savedNumberUrl = url;
      savedNumberPayload = JSON.parse(options.body);
      return {channel:{..._persistentChannels[0], channel_number:'8'}};
    };
  `);
  await p.run('savePersistentNumber(7, inlineInput)');
  assert.equal(p.run('savedNumberUrl'), '/api/xtream/persistent-channels/7');
  assert.equal(p.run('savedNumberPayload.channel_number'), '8');
  assert.equal(p.run('_persistentChannels[0].channel_number'), '8');
});

test('drag submits the complete channel order with original numbers', async () => {
  const p = page();
  const rows = [3, 1, 2].map(id => ({dataset:{channelId:String(id)},
    querySelector() { return {value:{1:'2', 2:'7.5', 3:'20'}[id]}; }}));
  p.document.getElementById('persistent-list').querySelectorAll = () => rows;
  p.run(`
    _persistentNumberMode = true;
    _persistentChannels = [
      {id:1,channel_number:'2',display_name:'One'},
      {id:2,channel_number:'7.5',display_name:'Two'},
      {id:3,channel_number:'20',display_name:'Three'}
    ];
    persistentRequest = async (url, options) => {
      savedOrderUrl = url;
      savedOrder = JSON.parse(options.body).order;
      return {channels:_persistentChannels};
    };
  `);
  await p.run('savePersistentOrder()');
  assert.equal(p.run('savedOrderUrl'), '/api/xtream/persistent-channels/reorder');
  assert.deepEqual(JSON.parse(JSON.stringify(p.run('savedOrder'))), [
    {id:3,channel_number:'20'}, {id:1,channel_number:'2'}, {id:2,channel_number:'7.5'},
  ]);
});
