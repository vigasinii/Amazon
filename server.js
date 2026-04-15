const express  = require('express');
const { spawn } = require('child_process');
const fs        = require('fs');
const path      = require('path');
const cors      = require('cors');

const app  = express();
const PORT = 3000;
const DATA_FILE = path.join(__dirname, 'scraped_data.json');

app.use(cors());
app.use(express.json());
app.use(express.static(__dirname)); // serves dashboard.html, scraped_data.json etc

// ── In-memory job store ──────────────────────────────────────────────
// { [jobId]: { status, asin, log, startedAt, finishedAt, error } }
const jobs = {};
let jobCounter = 0;

function newJobId() { return `job_${++jobCounter}_${Date.now()}`; }

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

app.listen(PORT, () => {
  console.log(`\n  ✅ PriceIQ server running at http://localhost:${PORT}`);
  console.log(`  📄 Open http://localhost:${PORT}/dashboard.html`);
  console.log(`  🔌 API at http://localhost:${PORT}/api/\n`);
});