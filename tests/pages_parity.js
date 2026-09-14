// Node: build the fixture bundle with docs/report.js and print it (timestamp pinned) for diffing
// against the Python output. Usage: node tests/pages_parity.js fixture.json
const fs = require('fs');
const SR = require('../docs/report.js');
const d = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
d.bye_teams = d.bye_teams === null ? null : new Set(d.bye_teams);
process.stdout.write(SR.buildBundle(d, new Date(0)));
