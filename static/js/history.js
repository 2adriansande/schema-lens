window.handleGlobalSearch = function(query) {
  const input = document.getElementById('searchInput');
  if (!input) return;
  input.value = query;
  filterQueries();
};

(function () {
  const params = new URLSearchParams(window.location.search);
  const initialQuery = params.get('q');
  if (initialQuery) {
    window.handleGlobalSearch(initialQuery);
  }
})();

let currentFilter = 'all';
function setFilter(f) {
  currentFilter = f;
  ['all','success','error'].forEach(t => {
    document.getElementById(`tab-${t}`).classList.toggle('active', t === f);
  });
  filterQueries();
}
function filterQueries() {
  const q = document.getElementById('searchInput').value.toLowerCase();
  document.querySelectorAll('.qh-card').forEach(card => {
    const matchText   = card.dataset.query.includes(q);
    const matchFilter = currentFilter === 'all' || card.dataset.status === currentFilter;
    card.style.display = (matchText && matchFilter) ? '' : 'none';
  });
}
async function deleteQuery(id) {
  if (!confirm('Delete this query?')) return;
  const r = await fetch(`/history/${id}/delete`, {method:'POST'});
  if (r.ok) {
    const el = document.getElementById(`qcard-${id}`);
    el.style.transition = 'opacity .25s, transform .25s';
    el.style.opacity = '0'; el.style.transform = 'translateX(12px)';
    setTimeout(() => el.remove(), 250);
  }
}

async function clearAllQueries() {
  if (!confirm('Delete all query history? This cannot be undone.')) return;
  const r = await fetch('/history/clear-all', {method:'POST'});
  if (r.ok) {
    const queryList = document.getElementById('queryList');
    queryList.innerHTML = `
      <div class="empty">
        <i class="bi bi-clock-history"></i>
        <h4>No queries yet</h4>
        <p>Your generated SQL queries will appear here.</p>
        <a href="{{ url_for('query_page') }}" class="btn btn-primary mt-3" style="display:inline-flex;">Generate First Query</a>
      </div>
    `;
    const pagination = document.querySelector('div[style*="justify-content:center"]');
    if (pagination) pagination.remove();
  }
}

function copySQL(id) {
  const card = document.getElementById(`qcard-${id}`);
  const sql = card.querySelector('.qh-sql').textContent.trim();
  navigator.clipboard.writeText(sql);
}