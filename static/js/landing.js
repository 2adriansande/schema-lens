document.addEventListener('DOMContentLoaded', () => {

  // ==========================================================================
  // ANTIGRAVITY INTERACTIVE BACKGROUND ENGINE
  // ==========================================================================
  const canvas = document.getElementById('antigravity-canvas');
  const ctx = canvas.getContext('2d');

  let particles = [];
  let mouse = { x: null, y: null, radius: 160 }; // Forcefield threshold radius

  // Match density to client viewport size
  function resizeCanvas() {
    canvas.width = window.innerWidth;
    canvas.height = window.innerHeight;
    initParticles();
  }

  class Particle {
    constructor() {
      this.x = Math.random() * canvas.width;
      this.y = Math.random() * canvas.height;
      this.radius = 1.5 + Math.random() * 2.5;
      
      // Base natural drift vectors (floating gently upward)
      this.baseSpeedY = -(0.1 + Math.random() * 0.3);
      this.baseSpeedX = (Math.random() - 0.5) * 0.1;
      
      this.vx = this.baseSpeedX;
      this.vy = this.baseSpeedY;
      
      // Dynamic opacity tracking for entry/exit fades
      this.opacity = 0.08 + Math.random() * 0.25;
      
      // Organic branding hue variants matching your design tokens (#708993 & #A1C2BD)
      const colors = ['rgba(112, 137, 147, ', 'rgba(161, 194, 189, ', 'rgba(25, 24, 59, '];
      this.colorBase = colors[Math.floor(Math.random() * colors.length)];
    }

    draw() {
      ctx.beginPath();
      ctx.arc(this.x, this.y, this.radius, 0, Math.PI * 2);
      ctx.fillStyle = `${this.colorBase}${this.opacity})`;
      ctx.fill();
    }

    update() {
      // Float naturally
      this.x += this.vx;
      this.y += this.vy;

      // Wrap around boundaries cleanly if out of viewport
      if (this.y < -20) {
        this.y = canvas.height + 20;
        this.x = Math.random() * canvas.width;
      }
      if (this.x < -20) this.x = canvas.width + 20;
      if (this.x > canvas.width + 20) this.x = -20;

      // Mouse Proximity Vector Math Engine (The Antigravity effect)
      if (mouse.x !== null && mouse.y !== null) {
        let dx = this.x - mouse.x;
        let dy = this.y - mouse.y;
        let distance = Math.hypot(dx, dy);

        if (distance < mouse.radius) {
          // Calculate push vector force scaled exponentially by proximity
          let force = (mouse.radius - distance) / mouse.radius;
          let forceX = (dx / distance) * force * 6;
          let forceY = (dy / distance) * force * 6;

          // Apply forcefield acceleration vectors
          this.vx += forceX;
          this.vy += forceY;
        } else {
          // Smooth structural drag return to base drift values over time
          this.vx += (this.baseSpeedX - this.vx) * 0.04;
          this.vy += (this.baseSpeedY - this.vy) * 0.04;
        }
      } else {
        // Smooth transition back if mouse leaves layout frame completely
        this.vx += (this.baseSpeedX - this.vx) * 0.04;
        this.vy += (this.baseSpeedY - this.vy) * 0.04;
      }
    }
  }

  function initParticles() {
    particles = [];
    // Balanced particle count to match screenshot density without lowering FPS
    const particleCount = Math.floor((canvas.width * canvas.height) / 7000);
    for (let i = 0; i < particleCount; i++) {
      particles.push(new Particle());
    }
  }

  function animateParticles() {
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    for (let i = 0; i < particles.length; i++) {
      particles[i].update();
      particles[i].draw();
    }
    requestAnimationFrame(animateParticles);
  }

  // Event bindings for interaction vectors
  window.addEventListener('mousemove', (e) => {
    mouse.x = e.clientX;
    mouse.y = e.clientY;
  });

  window.addEventListener('mouseleave', () => {
    mouse.x = null;
    mouse.y = null;
  });

  window.addEventListener('resize', resizeCanvas);
  
  // Fire initialization sequence
  resizeCanvas();
  animateParticles();


  // --- RESPONSIVE MOBILE NAVIGATION DRAWER ---
  const navbar = document.getElementById('navbar');
  const menuToggleBtn = document.getElementById('menuToggleBtn');
  
  menuToggleBtn.addEventListener('click', () => {
    navbar.classList.toggle('mobile-open');
    const icon = menuToggleBtn.querySelector('i');
    if(navbar.classList.contains('mobile-open')) {
      icon.className = 'bi bi-x';
    } else {
      icon.className = 'bi bi-list';
    }
  });


  // ==========================================================================
  // DYNAMIC TERMINAL TYPEWRITER & SYNTAX HIGHLIGHTING ENGINE
  // ==========================================================================
  const typewriterText = document.getElementById('typewriterText');
  const sqlTerminalCode = document.getElementById('sqlTerminalCode');
  const statusDot = document.getElementById('statusDot');
  const statusLabel = document.getElementById('statusLabel');
  const modelLabel = document.getElementById('modelLabel');

  const pipelineLoops = [
    {
      naturalQuery: "Find the top 5 customers with the highest lifetime order values in 2025",
      sqlRaw: "SELECT c.id, c.name, SUM(o.total_amount) AS lifetime_value\nFROM customers c\nJOIN orders o ON c.id = o.customer_id\nWHERE o.order_date >= '2025-01-01'\nGROUP BY c.id\nORDER BY lifetime_value DESC\nLIMIT 5;",
      metrics: "2.4ms"
    },
    {
      naturalQuery: "Get active users who haven't logged in within the last 30 days",
      sqlRaw: "SELECT user_id, email, last_login\nFROM users\nWHERE status = 'active'\n  AND last_login < CURRENT_DATE - INTERVAL '30 days'\nORDER BY last_login ASC;",
      metrics: "1.8ms"
    },
    {
      naturalQuery: "List product categories with low inventory count less than 10 items remaining",
      sqlRaw: "SELECT p.category, COUNT(*) AS low_stock_count\nFROM products p\nWHERE p.stock_quantity < 10\nGROUP BY p.category\nHAVING COUNT(*) > 0;",
      metrics: "3.1ms"
    }
  ];

  let loopIndex = 0;

  // Simple runtime syntax color tokenizer for SQL
  function tokenHighlighter(text) {
    // Regex sequences for strings, numbers, comments, and variables
    let highlighted = text
      .replace(/(-- .*)/g, '<span class="sql-comment">$1</span>')
      .replace(/('([^'\\]|\\.)*')/g, '<span class="sql-string">$1</span>')
      .replace(/\b(\d+)\b/g, '<span class="sql-number">$1</span>');

    // Keywords token map
    const keywords = [
      'SELECT', 'FROM', 'JOIN', 'ON', 'WHERE', 'GROUP BY', 'HAVING', 
      'ORDER BY', 'LIMIT', 'DESC', 'ASC', 'AND', 'OR', 'AS', 'INTERVAL'
    ];
    keywords.forEach(keyword => {
      const regex = new RegExp(`\\b${keyword}\\b`, 'g');
      highlighted = highlighted.replace(regex, `<span class="sql-keyword">${keyword}</span>`);
    });

    // Functions token map
    const functions = ['SUM', 'COUNT', 'CURRENT_DATE'];
    functions.forEach(func => {
      const regex = new RegExp(`\\b${func}\\b`, 'g');
      highlighted = highlighted.replace(regex, `<span class="sql-function">${func}</span>`);
    });

    return highlighted;
  }

  function simulateStreamPipeline() {
    const currentLoop = pipelineLoops[loopIndex];
    let queryCharIndex = 0;
    
    // Reset Terminal UI State
    typewriterText.textContent = "";
    typewriterText.classList.add('typewriter-cursor');
    sqlTerminalCode.innerHTML = '<span class="sql-comment">-- Listening for incoming stream buffers...</span>';
    
    statusDot.style.background = "#b45309";
    statusLabel.style.color = "#92400e";
    statusLabel.textContent = "Awaiting input stream";
    modelLabel.textContent = "llama3.2-3b · --";

    // Step 1: Type Natural Language Prompt
    function typeNaturalQuery() {
      if (queryCharIndex < currentLoop.naturalQuery.length) {
        typewriterText.textContent += currentLoop.naturalQuery.charAt(queryCharIndex);
        queryCharIndex++;
        setTimeout(typeNaturalQuery, 35);
      } else {
        typewriterText.classList.remove('typewriter-cursor');
        setTimeout(triggerPipelineAnalysis, 600);
      }
    }

    // Step 2: Transition into Schema Mapping Phase
    function triggerPipelineAnalysis() {
      statusDot.style.background = "#2563eb";
      statusLabel.style.color = "#1e40af";
      statusLabel.textContent = "Analyzing schema trees";
      sqlTerminalCode.innerHTML = '<span class="sql-comment">-- Resolving foreign relations and table nodes...</span>';
      
      setTimeout(streamSQLOutput, 1000);
    }

    // Step 3: Stream out dynamic token highlighted SQL matching typewriter output design standard
    function streamSQLOutput() {
      statusDot.style.background = "#059669";
      statusLabel.style.color = "#065f46";
      statusLabel.textContent = "Generating query structural code";
      
      let sqlCharIndex = 0;
      sqlTerminalCode.innerHTML = "";

      function appendSqlTokenStream() {
        if (sqlCharIndex <= currentLoop.sqlRaw.length) {
          const currentSubstr = currentLoop.sqlRaw.substring(0, sqlCharIndex);
          sqlTerminalCode.innerHTML = tokenHighlighter(currentSubstr) + '<span class="sql-cursor"></span>';
          sqlCharIndex += 2; // Speed up formatting matrix print rates slightly
          setTimeout(appendSqlTokenStream, 15);
        } else {
          // Finalize render, drop raw flashing cursor blocks, apply telemetry logs
          sqlTerminalCode.innerHTML = tokenHighlighter(currentLoop.sqlRaw);
          modelLabel.textContent = `llama3.2-3b · ${currentLoop.metrics}`;
          statusLabel.textContent = "Query generation optimization success";
          
          // Hold full screen query visibility before cycling to the next example
          loopIndex = (loopIndex + 1) % pipelineLoops.length;
          setTimeout(simulateStreamPipeline, 4500);
        }
      }
      appendSqlTokenStream();
    }

    typeNaturalQuery();
  }

  // Warmup delay before typewriter engine runs loop
  setTimeout(simulateStreamPipeline, 800);


  // --- INTERSECTION OBSERVER SCROLL REVEAL ENGINE ---
  const revealElements = document.querySelectorAll('.scroll-reveal');
  const observerOptions = { threshold: 0.1, rootMargin: "0px 0px -40px 0px" };

  const revealObserver = new IntersectionObserver((entries, observer) => {
    entries.forEach(entry => {
      if (entry.isIntersecting) {
        entry.target.classList.add('revealed');
        observer.unobserve(entry.target); // Optimize processing pipelines by dropping observation bounds once visible
      }
    });
  }, observerOptions);

  revealElements.forEach(element => revealObserver.observe(element));
});