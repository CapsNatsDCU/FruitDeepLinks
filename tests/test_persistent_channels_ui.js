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


test('guide source changes reset pagination and preserve unsaved selection', async () => {
  const p = page();
  p.run(`
    showPersistentEdit(7);
    const target = document.getElementById('persistent-form-epg-links'); target.isConnected = true;
    document.getElementById('persistent-form-epg-query').value = 'ESPN';
    document.getElementById('persistent-form-epg-mode').value = 'similar';
    document.getElementById('persistent-form-epg-filter').value = 'all';
    document.getElementById('persistent-form-epg-source').value = 'xmltv:1:chosen';
    document.getElementById('persistent-form-name').value = 'Unsaved channel name';
    document.getElementById('persistent-form-epg-search').isConnected = true;
    const urls = [];
    persistentRequest = async url => { urls.push(url); return {candidates:[], cache:{refreshed_at:'now'}}; };
  `);
  await p.run("findSetupEpgLinks(document.getElementById('persistent-form-epg-search'))");
  await p.run("findSetupEpgLinks(document.getElementById('persistent-form-epg-search'), 50)");
  p.run("document.getElementById('persistent-form-epg-filter').value = 'xmltv:2'");
  await p.run("findSetupEpgLinks(document.getElementById('persistent-form-epg-search'), 50)");
  assert.equal(new URL(p.run('urls[1]'), 'http://fixture').searchParams.get('offset'), '50');
  const filtered = new URL(p.run('urls[2]'), 'http://fixture').searchParams;
  assert.equal(filtered.get('source'), 'xmltv:2');
  assert.equal(filtered.get('offset'), '0');
  assert.equal(p.document.getElementById('persistent-form-epg-source').value, 'xmltv:1:chosen');
  assert.equal(p.document.getElementById('persistent-form-name').value, 'Unsaved channel name');
});

test('team guide config survives polling and saving avoids provider refresh', async () => {
  const p = page();
  p.run(`_teamScheduleTeams = [{key:'nhl|washington capitals',team:'Washington Capitals',league:'NHL'}]; showPersistentEdit(7);`);
  const values = {
    'persistent-form-guide-mode':'team', 'persistent-form-schedule-team':'nhl|washington capitals',
    'persistent-form-pre':'15', 'persistent-form-post':'45', 'persistent-form-duration':'210',
    'persistent-form-name':'Capitals', 'persistent-form-number':'7', 'persistent-form-enabled':'true',
  };
  for (const [id,value] of Object.entries(values)) p.document.getElementById(id).value=value;
  p.run('teamGuideChanged()');
  assert.equal(p.document.getElementById('persistent-standard-guide').hidden,true);
  assert.equal(p.document.getElementById('persistent-standard-guide-id').hidden,true);
  assert.equal(p.document.getElementById('persistent-team-guide').hidden,false);
  await p.run('loadQualityQueue()');
  assert.equal(p.run('readPersistentForm().team_duration_minutes'),210);
  p.run(`requests = []; persistentRequest = async (url, options) => {
    requests.push(url);
    if(options) { capturedSave=JSON.parse(options.body); return {channel:{display_name:'Capitals',enabled:true,guide_mode:'team'}}; }
    return {channels:[]};
  };`);
  await p.run('savePersistentForm("edit",7)');
  assert.equal(p.run('capturedSave.guide_mode'),'team');
  assert.equal(p.run('capturedSave.team_pre_minutes'),15);
  assert.equal(p.run('requests.includes("/api/xtream/epg/refresh")'),false);
});

test('changing team settings rejects an outdated preview response', async () => {
  const p = page();
  p.context.Date = Date;
  p.document.getElementById('persistent-team-preview').isConnected=true;
  p.run(`persistentRequest = () => new Promise(resolve => {resolvePreview=resolve});`);
  const button={disabled:false,isConnected:true};
  p.context.fixtureButton=button;
  const pending=p.run('previewTeamGuide(fixtureButton)');
  assert.equal(button.disabled,true);
  assert.equal(p.document.getElementById('persistent-team-preview').textContent,'Building guide preview…');
  p.run('teamGuideChanged(); resolvePreview({schedule_status:"ready",programmes:[]})');
  await pending;
  assert.equal(p.document.getElementById('persistent-team-preview').textContent,'');
  assert.equal(button.disabled,false);
});

test('channel names suggest a clear team but do not enable team mode or replace a saved selection', () => {
  const p=page();
  p.run(`_teamScheduleTeams = [
    {key:'nhl|washington capitals',team:'Washington Capitals',league:'NHL',aliases:['Caps']},
    {key:'nhl|pittsburgh penguins',team:'Pittsburgh Penguins',league:'NHL'},
    {key:'nfl|new york giants',team:'New York Giants',league:'NFL',aliases:['Giants']},
    {key:'mlb|san francisco giants',team:'San Francisco Giants',league:'MLB',aliases:['Giants']},
  ];`);
  assert.equal(p.run('guessScheduleTeam("US: WASHINGTON CAPITALS HD").team.key'),'nhl|washington capitals');
  assert.equal(p.run('guessScheduleTeam("US: Caps 4K").team.key'),'nhl|washington capitals');
  assert.equal(p.run('guessScheduleTeam("Giants").team'),null);
  assert.equal(p.run('guessScheduleTeam("Giants").ambiguous'),true);
  assert.equal(p.run('guessScheduleTeam("US: NFL Giants HD").team.key'),'nfl|new york giants');
  assert.equal(p.run('guessScheduleTeam("Washington Capitals vs Pittsburgh Penguins").team'),null);
  assert.equal(p.run('guessScheduleTeam("Capital One Sports").team'),null);
  const form=p.run('persistentFormHtml({display_name:"US: WASHINGTON CAPITALS",enabled:true},"add")');
  assert.ok(form.includes('Suggested from channel name: NHL'));
  assert.ok(form.includes('value="nhl|washington capitals" selected'));
  assert.ok(form.includes('value="standard" selected'));
  const saved=p.run('persistentFormHtml({display_name:"Washington Capitals",team_schedule_key:"nhl|pittsburgh penguins"},"edit")');
  assert.ok(saved.includes('value="nhl|pittsburgh penguins" selected'));
});

test('team search filters the catalog and keeps an existing selected team', () => {
  const p=page();
  p.run(`_teamScheduleTeams=[{key:'caps',team:'Washington Capitals',league:'NHL'}, {key:'wizards',team:'Washington Wizards',league:'NBA'}];`);
  const filtered=p.run('teamScheduleOptions("", "NHL Capitals")');
  assert.ok(filtered.includes('Washington Capitals'));
  assert.ok(!filtered.includes('Washington Wizards'));
  const selected=p.run('teamScheduleOptions("wizards", "NHL")');
  assert.ok(selected.includes('value="wizards" selected'));
});
