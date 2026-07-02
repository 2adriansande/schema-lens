// 1. Serialize server arrays into JSON
const topXpData      = JSON.parse('{{ (top_xp[:3]|tojson if top_xp else "[]") | safe }}');
const topQueriesData = JSON.parse('{{ (top_queries[:3]|tojson if top_queries else "[]") | safe }}');
const topSchemasData = JSON.parse('{{ (top_schemas[:3]|tojson if top_schemas else "[]") | safe }}');
const topBadgesData  = JSON.parse('{{ (top_badges[:3]|tojson if top_badges else "[]") | safe }}');
const topScoresData  = JSON.parse('{{ (top_scores[:3]|tojson if top_scores else "[]") | safe }}');

const currentUserId  = JSON.parse('{{ (current_user.id|tojson if current_user and current_user.id else "null") | safe }}');

function mapPodiumRows(dataArray, metaKey, metaSuffix = '') {
  return dataArray.map(r => {
    const nameStr = r.name || "?";
    return {
      name: r.name,
      init: nameStr.charAt(0).toUpperCase(),
      lvl: r.level || undefined,
      meta: r[metaKey] !== undefined ? `${r[metaKey]}${metaSuffix}` : "",
      isMe: r.id === currentUserId
    };
  });
}

const LB = {
  xp: { title: "XP &amp; Level Podium", rows: mapPodiumRows(topXpData, 'xp', ' XP') },
  queries: { title: "Queries Podium", rows: mapPodiumRows(topQueriesData, 'query_count', ' Queries') },
  schemas: { title: "Schemas Podium", rows: mapPodiumRows(topSchemasData, 'schema_count', ' Schemas') },
  badges: { title: "Badges Podium", rows: mapPodiumRows(topBadgesData, 'badge_count', ' Badges') },
  scores: { title: "Avg Score Podium", rows: mapPodiumRows(topScoresData, 'avg_score', ' pts avg') }
};

const POS_RANK    = [2,    1,    3   ];
const POS_H       = [90,   120,  65  ];
const POS_BLOCK   = [
  'linear-gradient(160deg,#e2e8f0,#94a3b8)',
  'linear-gradient(160deg,#fef08a,#eab308)',
  'linear-gradient(160deg,#fed7aa,#c2440a)'
];
const POS_AV      = [
  'linear-gradient(135deg,#94a3b8,#64748b)',
  'linear-gradient(135deg,#eab308,#ca8a04)',
  'linear-gradient(135deg,#c2440a,#9a3412)'
];
const POS_NUM_BG    = ['#94a3b8', '#eab308', '#c2440a'];
const POS_NUM_COLOR = ['#1e293b', '#713f12', '#fff'   ];
const POS_LVL_BG    = ['#94a3b8', '#eab308', '#c2440a'];
const POS_LVL_COLOR = ['#1e293b', '#713f12', '#fff'   ];
const POS_BLOCK_NUM_COLOR = ['#1e293b','#713f12','#431407'];

function buildSlot(posIdx, row, key) {
  const rank    = POS_RANK[posIdx];
  const isEmpty = !row;

  const crown = (rank === 1 && !isEmpty)
    ? `<span class="crown-wrap"><svg viewBox="0 0 24 18" fill="none"><path d="M1 16L5 5L10 11L12 2L14 11L19 5L23 16H1Z" fill="#EAB308" stroke="#CA8A04" stroke-width="1.2" stroke-linejoin="round"/></svg></span>`
    : '';

  const avBg = isEmpty ? 'background:rgba(112,137,147,.14)' : `background:${POS_AV[posIdx]}`;
  const rankCircle = isEmpty ? '' : `<span class="p-rank-circle" style="background:${POS_NUM_BG[posIdx]};color:${POS_NUM_COLOR[posIdx]}">${rank}</span>`;
  const lvlPill = (key === 'xp' && !isEmpty && row.lvl)
    ? `<div class="p-lvl-wrap"><span class="p-lvl" style="background:${POS_LVL_BG[posIdx]};color:${POS_LVL_COLOR[posIdx]}">LVL ${row.lvl}</span></div>`
    : '<div class="p-lvl-wrap" style="height:18px"></div>';

  const initChar = isEmpty ? '?' : row.init;
  const youDot   = (!isEmpty && row.isMe) ? `<span class="you-dot"></span>` : '';
  const nameHtml = isEmpty ? '—' : `${row.name}${youDot}`;
  const metaHtml = isEmpty ? '' : (() => {
    const p = row.meta.split(' ');
    return `<strong>${p[0]}</strong> ${p.slice(1).join(' ')}`;
  })();

  return `
<div class="podium-slot ${isEmpty ? 'p-empty' : ''}">
  <div class="p-avatar-wrap">
    ${crown}
    <div class="p-avatar" style="${avBg}">${initChar}</div>
    ${rankCircle}
  </div>
  ${lvlPill}
  <div class="p-name">${nameHtml}</div>
  <div class="p-meta">${metaHtml}</div>
  <div class="podium-block" style="height:${POS_H[posIdx]}px;background:${isEmpty ? 'rgba(112,137,147,.1)' : POS_BLOCK[posIdx]}">
    <div class="podium-block-num" style="color:${isEmpty ? 'rgba(112,137,147,.2)' : POS_BLOCK_NUM_COLOR[posIdx]}">${rank}</div>
  </div>
</div>`;
}

function renderPodium(key) {
  const board = LB[key];
  if (!board) return;
  document.getElementById('podium-title').innerHTML = board.title;
  const rowOrder = [1, 0, 2];
  let html = '';
  rowOrder.forEach((rowIdx, posIdx) => {
    html += buildSlot(posIdx, board.rows[rowIdx] || null, key);
  });
  document.getElementById('podium-stage').innerHTML = html;
}

function switchTab(btn, panel) {
  document.querySelectorAll('.lb-tab').forEach(t => t.classList.remove('active'));
  document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
  btn.classList.add('active');
  document.getElementById('panel-' + panel).classList.add('active');
  renderPodium(panel);
}

document.addEventListener('DOMContentLoaded', () => renderPodium('xp'));