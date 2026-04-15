const express  = require('express');
const { spawn } = require('child_process');
const fs        = require('fs');
const path      = require('path');
const cors      = require('cors');

const app  = express();
const PORT = process.env.PORT || 3000;
const DATA_FILE     = path.join(__dirname, 'scraped_data.json');
const SCHEDULE_FILE = path.join(__dirname, 'schedule.json');

app.use(cors());
app.use(express.json());

// ── Basic Auth ────────────────────────────────────────────────────────
const AUTH_USER = process.env.DASHBOARD_USER || 'admin';
const AUTH_PASS = process.env.DASHBOARD_PASS || 'priceiq123';

app.use((req, res, next) => {
  // Allow API calls without auth if you want (optional — remove this block to lock API too)
  const authHeader = req.headers['authorization'];
  if (!authHeader || !authHeader.startsWith('Basic ')) {
    res.setHeader('WWW-Authenticate', 'Basic realm="PriceIQ"');
    return res.status(401).send('Authentication required');
  }
  const base64 = authHeader.slice(6);
  const [user, pass] = Buffer.from(base64, 'base64').toString().split(':');
  if (user !== AUTH_USER || pass !== AUTH_PASS) {
    res.setHeader('WWW-Authenticate', 'Basic realm="PriceIQ"');
    return res.status(401).send('Invalid credentials');
  }
  next();
});

app.use(express.static(__dirname));

// Redirect root to dashboard
app.get('/', (req, res) => {
  res.redirect('/dashboard.html');
});

// ── In-memory job store ──────────────────────────────────────────────
const jobs = {};
let jobCounter = 0;
function newJobId() { return `job_${++jobCounter}_${Date.now()}`; }

// ── Scheduler state ──────────────────────────────────────────────────
let scheduleTimer   = null;
let schedulerStatus = { running: false, intervalMins: 0, nextRunAt: null, lastRunAt: null, currentlyRunning: false };

function loadScheduleConfig() {
  try {
    if (fs.existsSync(SCHEDULE_FILE)) return JSON.parse(fs.readFileSync(SCHEDULE_FILE, 'utf8'));
  } catch(e) {}
  return { enabled: false, intervalMins: 15 };
}

function saveScheduleConfig(cfg) {
  fs.writeFileSync(SCHEDULE_FILE, JSON.stringify(cfg, null, 2));
}

// Run all products in scraped_data.json through the scraper sequentially
async function runScheduledScrape() {
  if (schedulerStatus.currentlyRunning) {
    console.log('  [Scheduler] Skipping — previous scrape still running');
    return;
  }
  if (!fs.existsSync(DATA_FILE)) {
    console.log('  [Scheduler] No scraped_data.json yet — skipping');
    return;
  }

  let data;
  try { data = JSON.parse(fs.readFileSync(DATA_FILE, 'utf8')); }
  catch(e) { console.log('  [Scheduler] Failed to read data file:', e.message); return; }

  const products = data.products || [];
  if (!products.length) {
    console.log('  [Scheduler] No products to scrape');
    return;
  }

  schedulerStatus.currentlyRunning = true;
  schedulerStatus.lastRunAt = new Date().toISOString();
  console.log(`\n  [Scheduler] Starting scrape of ${products.length} product(s) at ${schedulerStatus.lastRunAt}`);

  for (const p of products) {
    await new Promise((resolve) => {
      const jobId = newJobId();
      jobs[jobId] = {
        status: 'running', asin: p.asin, comp_mode: p.comp_mode || 'brand',
        log: [], startedAt: new Date().toISOString(), finishedAt: null,
        error: null, scheduled: true
      };

      const args = [
        'scraper.py', p.asin,
        String(p.my_price || p.price || 0),
        String(p.cost || 0),
        String(p.floor || 0),
        String(p.map || 0),
        '--comp-mode', p.comp_mode || 'brand'
      ];

      console.log(`  [Scheduler] Scraping ${p.asin}...`);
      const proc = spawn('python', args, { cwd: __dirname });
      proc.stdout.on('data', chunk => chunk.toString().split('\n').filter(Boolean).forEach(l => jobs[jobId].log.push(l)));
      proc.stderr.on('data', chunk => chunk.toString().split('\n').filter(Boolean).forEach(l => jobs[jobId].log.push('ERR: '+l)));
      proc.on('close', code => {
        jobs[jobId].finishedAt = new Date().toISOString();
        jobs[jobId].status = code === 0 ? 'done' : 'failed';
        if (code !== 0) jobs[jobId].error = `Exit code ${code}`;
        console.log(`  [Scheduler] ${p.asin} → ${jobs[jobId].status}`);
        resolve();
      });
    });
  }

  schedulerStatus.currentlyRunning = false;
  schedulerStatus.lastRunAt = new Date().toISOString();
  console.log(`  [Scheduler] Cycle complete at ${schedulerStatus.lastRunAt}\n`);
}

function startScheduler(intervalMins) {
  stopScheduler();
  if (!intervalMins || intervalMins < 1) return;

  const ms = intervalMins * 60 * 1000;
  schedulerStatus.running      = true;
  schedulerStatus.intervalMins = intervalMins;
  schedulerStatus.nextRunAt    = new Date(Date.now() + ms).toISOString();

  scheduleTimer = setInterval(async () => {
    schedulerStatus.nextRunAt = new Date(Date.now() + ms).toISOString();
    await runScheduledScrape();
  }, ms);

  console.log(`  [Scheduler] Started — every ${intervalMins} min(s). Next run: ${schedulerStatus.nextRunAt}`);
}

function stopScheduler() {
  if (scheduleTimer) { clearInterval(scheduleTimer); scheduleTimer = null; }
  schedulerStatus.running      = false;
  schedulerStatus.nextRunAt    = null;
  schedulerStatus.intervalMins = 0;
}

// Auto-start scheduler if previously configured
const savedConfig = loadScheduleConfig();
if (savedConfig.enabled && savedConfig.intervalMins > 0) {
  console.log(`  [Scheduler] Resuming saved schedule: every ${savedConfig.intervalMins} min(s)`);
  startScheduler(savedConfig.intervalMins);
}

// ── GET /api/data ─────────────────────────────────────────────────────
app.get('/api/data', (req, res) => {
  if (!fs.existsSync(DATA_FILE))
    return res.json({ products: [], total: 0, success: 0, success_rate: 0, scraped_at: null });
  try { res.json(JSON.parse(fs.readFileSync(DATA_FILE, 'utf8'))); }
  catch(e) { res.status(500).json({ error: 'Failed to read scraped_data.json' }); }
});

// ── POST /api/scrape ── manual scrape ────────────────────────────────
app.post('/api/scrape', (req, res) => {
  const { asin, my_price=0, cost=0, floor=0, map=0, comp_mode='brand' } = req.body;
  if (!asin || asin.length !== 10) return res.status(400).json({ error: 'Invalid ASIN' });

  const jobId = newJobId();
  jobs[jobId] = { status:'running', asin:asin.toUpperCase(), comp_mode, log:[], startedAt:new Date().toISOString(), finishedAt:null, error:null };
  res.json({ jobId, status: 'running' });

  const args = ['scraper.py', asin.toUpperCase(), String(my_price), String(cost), String(floor), String(map), '--comp-mode', comp_mode];
  const proc = spawn('python', args, { cwd: __dirname });
  proc.stdout.on('data', chunk => chunk.toString().split('\n').filter(Boolean).forEach(l => jobs[jobId].log.push(l)));
  proc.stderr.on('data', chunk => chunk.toString().split('\n').filter(Boolean).forEach(l => jobs[jobId].log.push('ERR: '+l)));
  proc.on('close', code => {
    jobs[jobId].finishedAt = new Date().toISOString();
    jobs[jobId].status = code === 0 ? 'done' : 'failed';
    if (code !== 0) jobs[jobId].error = `Process exited with code ${code}`;
  });
});

// ── GET /api/jobs/:id ─────────────────────────────────────────────────
app.get('/api/jobs/:id', (req, res) => {
  const job = jobs[req.params.id];
  if (!job) return res.status(404).json({ error: 'Job not found' });
  res.json(job);
});

// ── GET /api/jobs ─────────────────────────────────────────────────────
app.get('/api/jobs', (req, res) => {
  const list = Object.entries(jobs)
    .map(([id, j]) => ({ jobId:id, asin:j.asin, status:j.status, comp_mode:j.comp_mode, startedAt:j.startedAt, finishedAt:j.finishedAt, scheduled:j.scheduled||false }))
    .sort((a,b) => new Date(b.startedAt)-new Date(a.startedAt))
    .slice(0, 50);
  res.json(list);
});

// ── GET /api/scheduler ── get scheduler status ────────────────────────
app.get('/api/scheduler', (req, res) => {
  res.json({ ...schedulerStatus, config: loadScheduleConfig() });
});

// ── POST /api/scheduler ── start/stop/configure ───────────────────────
// Body: { action: 'start'|'stop'|'run_now', intervalMins: number }
app.post('/api/scheduler', async (req, res) => {
  const { action, intervalMins } = req.body;

  if (action === 'start') {
    const mins = parseInt(intervalMins) || 15;
    saveScheduleConfig({ enabled: true, intervalMins: mins });
    startScheduler(mins);
    res.json({ ok: true, status: schedulerStatus });
  } else if (action === 'stop') {
    stopScheduler();
    saveScheduleConfig({ enabled: false, intervalMins: parseInt(intervalMins) || 15 });
    res.json({ ok: true, status: schedulerStatus });
  } else if (action === 'run_now') {
    res.json({ ok: true, message: 'Scrape triggered' });
    await runScheduledScrape();
  } else {
    res.status(400).json({ error: 'Unknown action' });
  }
});

// ── DELETE /api/data/:asin ────────────────────────────────────────────
app.delete('/api/data/:asin', (req, res) => {
  const asin = req.params.asin.toUpperCase();
  try {
    if (!fs.existsSync(DATA_FILE)) return res.json({ ok: true });
    const data = JSON.parse(fs.readFileSync(DATA_FILE, 'utf8'));
    data.products = (data.products||[]).filter(p => p.asin !== asin);
    data.total        = data.products.length;
    data.success      = data.products.filter(p => p.scrape_status==='ok').length;
    data.success_rate = data.total ? Math.round(data.success/data.total*100) : 0;
    fs.writeFileSync(DATA_FILE, JSON.stringify(data, null, 2));
    res.json({ ok: true, remaining: data.products.length });
  } catch(e) { res.status(500).json({ error: e.message }); }
});

// ── DELETE /api/data ──────────────────────────────────────────────────
app.delete('/api/data', (req, res) => {
  try {
    if (fs.existsSync(DATA_FILE)) fs.unlinkSync(DATA_FILE);
    res.json({ ok: true });
  } catch(e) { res.status(500).json({ error: e.message }); }
});


// ── GET /api/data ── serve scraped_data.json ─────────────────────────
app.get('/api/data', (req, res) => {
  if (!fs.existsSync(DATA_FILE)) {
    return res.json({ products: [], total: 0, success: 0, success_rate: 0, scraped_at: null });
  }
  try {
    const raw = fs.readFileSync(DATA_FILE, 'utf8');
    res.json(JSON.parse(raw));
  } catch (e) {
    res.status(500).json({ error: 'Failed to read scraped_data.json' });
  }
});

// ── POST /api/scrape ── kick off a scrape job ────────────────────────
// Body: { asin, my_price, cost, floor, map, comp_mode }
// comp_mode: "brand" | "reseller" | "both"
app.post('/api/scrape', (req, res) => {
  const { asin, my_price=0, cost=0, floor=0, map=0, comp_mode='brand' } = req.body;

  if (!asin || asin.length !== 10) {
    return res.status(400).json({ error: 'Invalid ASIN — must be 10 characters' });
  }

  const jobId = newJobId();
  jobs[jobId] = {
    status: 'running',
    asin: asin.toUpperCase(),
    comp_mode,
    log: [],
    startedAt: new Date().toISOString(),
    finishedAt: null,
    error: null
  };

  // Return job ID immediately so frontend can poll
  res.json({ jobId, status: 'running' });

  // Spawn Python scraper in background
  const args = [
    'scraper.py',
    asin.toUpperCase(),
    String(my_price),
    String(cost),
    String(floor),
    String(map),
    '--comp-mode', comp_mode
  ];

  const proc = spawn('python', args, { cwd: __dirname });

  proc.stdout.on('data', chunk => {
    const lines = chunk.toString().split('\n').filter(Boolean);
    lines.forEach(l => jobs[jobId].log.push(l));
  });
  proc.stderr.on('data', chunk => {
    const lines = chunk.toString().split('\n').filter(Boolean);
    lines.forEach(l => jobs[jobId].log.push('ERR: ' + l));
  });
  proc.on('close', code => {
    jobs[jobId].finishedAt = new Date().toISOString();
    if (code === 0) {
      jobs[jobId].status = 'done';
    } else {
      jobs[jobId].status = 'failed';
      jobs[jobId].error  = `Process exited with code ${code}`;
    }
  });
});

// ── GET /api/jobs/:id ── poll job status ─────────────────────────────
app.get('/api/jobs/:id', (req, res) => {
  const job = jobs[req.params.id];
  if (!job) return res.status(404).json({ error: 'Job not found' });
  res.json(job);
});

// ── GET /api/jobs ── list all recent jobs ────────────────────────────
app.get('/api/jobs', (req, res) => {
  const list = Object.entries(jobs)
    .map(([id, j]) => ({ jobId: id, asin: j.asin, status: j.status, comp_mode: j.comp_mode, startedAt: j.startedAt, finishedAt: j.finishedAt }))
    .sort((a, b) => new Date(b.startedAt) - new Date(a.startedAt))
    .slice(0, 20);
  res.json(list);
});

// ── DELETE /api/data/:asin ── remove one product ─────────────────────
app.delete('/api/data/:asin', (req, res) => {
  const asin = req.params.asin.toUpperCase();
  try {
    if (!fs.existsSync(DATA_FILE)) return res.json({ ok: true });
    const raw  = fs.readFileSync(DATA_FILE, 'utf8');
    const data = JSON.parse(raw);
    data.products = (data.products || []).filter(p => p.asin !== asin);
    data.total        = data.products.length;
    data.success      = data.products.filter(p => p.scrape_status === 'ok').length;
    data.success_rate = data.total ? Math.round(data.success / data.total * 100) : 0;
    fs.writeFileSync(DATA_FILE, JSON.stringify(data, null, 2));
    res.json({ ok: true, remaining: data.products.length });
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

// ── DELETE /api/data ── clear all scraped data ───────────────────────
app.delete('/api/data', (req, res) => {
  try {
    if (fs.existsSync(DATA_FILE)) fs.unlinkSync(DATA_FILE);
    res.json({ ok: true });
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

app.listen(PORT, '0.0.0.0', () => {
  console.log(`\n  ✅ PriceIQ server running on port ${PORT}`);
  console.log(`  📄 Dashboard: http://localhost:${PORT}/dashboard.html\n`);
});
