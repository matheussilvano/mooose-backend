const API_BASE = "https://mooose-backend.onrender.com";

/* FRASES DE LOADING DIVERTIDAS */
const funnyMessages = [
  "Afiando o lápis virtual...",
  "Consultando os universitários...",
  "Colocando os óculos de leitura...",
  "Caçando erros de vírgula...",
  "Calculando sua nota 1000...",
  "Verificando a coesão...",
  "Analisando a proposta de intervenção..."
];

function showLoading(msg) {
  const overlay = document.getElementById("loading-overlay");
  const msgEl = document.getElementById("loading-msg");
  if (overlay) overlay.classList.remove("hidden");
  
  if (msgEl) {
    msgEl.textContent = msg || funnyMessages[0];
    if (msgEl.dataset.interval) clearInterval(msgEl.dataset.interval);
    let i = 0;
    msgEl.dataset.interval = setInterval(() => {
      i = (i + 1) % funnyMessages.length;
      msgEl.textContent = funnyMessages[i];
    }, 2500);
  }
}

function hideLoading() {
  const overlay = document.getElementById("loading-overlay");
  const msgEl = document.getElementById("loading-msg");
  if (overlay) overlay.classList.add("hidden");
  if (msgEl && msgEl.dataset.interval) clearInterval(msgEl.dataset.interval);
}

function showSection(id) {
  document.querySelectorAll(".section").forEach(s => s.classList.remove("visible"));
  const el = document.getElementById(id);
  if (el) el.classList.add("visible");
  window.scrollTo({ top: 0, behavior: 'smooth' });
}

/* AUTH & TOKEN */
function getToken() { return localStorage.getItem("token"); }
function setToken(t) { t ? localStorage.setItem("token", t) : localStorage.removeItem("token"); }
function getAuthHeaders(extra={}) {
  const t = getToken();
  return { "Content-Type": "application/json", ...(t ? { Authorization: `Bearer ${t}` } : {}), ...extra };
}

let updateTopbarUser = () => {};
let loadHistoricoFn = null;
let chartInstance = null;
let currentCredits = null;
let lastEssayId = null;
let lastReview = null;

function normalizeCredits(value) {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (typeof value === "string" && value.trim() !== "" && !Number.isNaN(Number(value))) return Number(value);
  return null;
}

function extractCredits(data) {
  if (!data) return null;
  const sources = [data, data.user, data.account, data.profile];
  const keys = [
    "credits",
    "creditos",
    "creditos_disponiveis",
    "credits_available",
    "correcoes_disponiveis",
    "corrections_left",
    "saldo_creditos",
    "credit_balance"
  ];
  for (const src of sources) {
    if (!src) continue;
    for (const key of keys) {
      const value = normalizeCredits(src[key]);
      if (value !== null) return value;
    }
  }
  return null;
}

function setCreditsUI(value) {
  currentCredits = value;
  document.querySelectorAll("[data-credit-balance]").forEach(el => {
    el.textContent = value === null ? "—" : value;
  });
}

function encodeAttr(value) {
  return encodeURIComponent(value ?? "");
}

function decodeAttr(value) {
  try { return value ? decodeURIComponent(value) : ""; } catch { return value || ""; }
}

function formatReviewDate(review) {
  const raw = review?.updated_at || review?.created_at || "";
  if (!raw) return "";
  const date = new Date(raw);
  if (Number.isNaN(date.getTime())) return "";
  return date.toLocaleDateString(undefined, { day: "2-digit", month: "2-digit", year: "numeric" });
}

function starText(value) {
  const filled = Math.min(5, Math.max(0, Number(value) || 0));
  return "★".repeat(filled) + "☆".repeat(5 - filled);
}

function renderStars(container, starsValue = 0) {
  if (!container) return;
  const value = Math.min(5, Math.max(0, Number(starsValue) || 0));
  container.dataset.value = value;
  container.innerHTML = Array.from({ length: 5 }, (_, idx) => {
    const star = idx + 1;
    const active = star <= value ? "active" : "";
    return `<button type="button" class="star-btn ${active}" data-star="${star}" aria-label="${star} estrela${star > 1 ? "s" : ""}">★</button>`;
  }).join("");
}

function initReviewWidget(widget) {
  if (!widget) return;
  widget.classList.remove("open");
  const stars = Number(widget.dataset.initialStars || 0);
  const comment = decodeAttr(widget.dataset.initialComment || "");
  const createdAt = decodeAttr(widget.dataset.initialCreatedAt || "");
  const updatedAt = decodeAttr(widget.dataset.initialUpdatedAt || "");
  renderStars(widget.querySelector("[data-review-stars]"), stars);
  const commentEl = widget.querySelector("[data-review-comment]");
  if (commentEl) commentEl.value = comment;
  updateReviewSummary(widget, {
    stars,
    comment,
    created_at: createdAt || null,
    updated_at: updatedAt || null
  });
  setReviewToggleLabel(widget, widget.classList.contains("open"));
}

function hydrateReviewWidgets(root = document) {
  root.querySelectorAll("[data-review-widget]").forEach(initReviewWidget);
}

function setReviewToggleLabel(widget, isOpen) {
  const toggle = widget?.querySelector("[data-review-toggle]");
  if (!toggle) return;
  if (isOpen) {
    toggle.textContent = "Fechar";
    return;
  }
  const hasReview = Number(widget.dataset.initialStars || 0) > 0;
  toggle.textContent = hasReview ? "Editar avaliação" : "Avaliar";
}

function updateReviewSummary(widget, review) {
  if (!widget) return;
  const summaryStars = widget.querySelector("[data-review-summary-stars]");
  const badge = widget.querySelector("[data-review-badge]");
  const hasReview = Number(review?.stars || 0) > 0;
  if (summaryStars) {
    summaryStars.textContent = hasReview ? `${starText(review.stars)} (${review.stars}/5)` : "Sem avaliação";
  }
  if (badge) {
    const dateLabel = formatReviewDate(review);
    if (dateLabel) {
      badge.textContent = `Avaliado em ${dateLabel}`;
      badge.classList.remove("hidden");
    } else {
      badge.textContent = "";
      badge.classList.add("hidden");
    }
  }
}

function updateResultadoReview(essayId, review) {
  const widget = document.getElementById("resultado-review");
  if (!widget) return;
  if (!essayId) {
    widget.classList.add("hidden");
    return;
  }
  widget.classList.remove("hidden");
  widget.dataset.essayId = essayId;
  widget.dataset.initialStars = review?.stars || 0;
  widget.dataset.initialComment = encodeAttr(review?.comment || "");
  widget.dataset.initialCreatedAt = encodeAttr(review?.created_at || "");
  widget.dataset.initialUpdatedAt = encodeAttr(review?.updated_at || "");
  initReviewWidget(widget);
}

async function submitReview(widget) {
  if (!widget) return;
  const msgEl = widget.querySelector("[data-review-msg]");
  if (msgEl) {
    msgEl.textContent = "";
    msgEl.className = "form-message";
  }
  const essayId = Number(widget.dataset.essayId || widget.closest("[data-essay-id]")?.dataset.essayId);
  const stars = Number(widget.querySelector("[data-review-stars]")?.dataset.value || 0);
  const comment = widget.querySelector("[data-review-comment]")?.value?.trim() || "";

  if (!essayId) {
    if (msgEl) {
      msgEl.textContent = "Não foi possível identificar a redação.";
      msgEl.className = "form-message error";
    }
    return;
  }
  if (!stars || stars < 1 || stars > 5) {
    if (msgEl) {
      msgEl.textContent = "Selecione de 1 a 5 estrelas.";
      msgEl.className = "form-message error";
    }
    return;
  }

  try {
    const res = await fetch(`${API_BASE}/app/enem/avaliar`, {
      method: "POST",
      headers: getAuthHeaders(),
      body: JSON.stringify({ essay_id: essayId, stars, comment: comment || undefined })
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      const serverMsg = data?.detail || data?.message || "Falha ao salvar avaliação.";
      throw new Error(serverMsg);
    }
    widget.dataset.initialStars = data.stars || stars;
    widget.dataset.initialComment = encodeAttr(data.comment || comment || "");
    if (data?.created_at || data?.updated_at) {
      widget.dataset.initialCreatedAt = encodeAttr(data.created_at || "");
      widget.dataset.initialUpdatedAt = encodeAttr(data.updated_at || "");
    }
    updateReviewSummary(widget, {
      stars: Number(widget.dataset.initialStars || stars),
      comment: decodeAttr(widget.dataset.initialComment || ""),
      created_at: decodeAttr(widget.dataset.initialCreatedAt || ""),
      updated_at: decodeAttr(widget.dataset.initialUpdatedAt || "")
    });
    widget.classList.remove("open");
    setReviewToggleLabel(widget, false);
    if (msgEl) {
      msgEl.textContent = "Avaliação salva!";
      msgEl.className = "form-message success";
    }
  } catch (err) {
    if (msgEl) {
      msgEl.textContent = err.message;
      msgEl.className = "form-message error";
    }
  }
}

async function startCheckout() {
  if (!getToken()) {
    if (typeof window.goToAuth === "function") {
      window.goToAuth("login");
    } else {
      showSection("section-auth");
    }
    return;
  }
  showLoading("Abrindo checkout...");
  try {
    const res = await fetch(`${API_BASE}/payments/create`, {
      method: "POST",
      headers: getAuthHeaders()
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      const msg = data?.detail || data?.message || "Falha ao iniciar o pagamento.";
      throw new Error(msg);
    }
    if (!data?.checkout_url) {
      throw new Error("Checkout indisponível. Tente novamente.");
    }
    window.location.href = data.checkout_url;
  } catch (err) {
    alert(err.message || "Erro ao iniciar pagamento.");
  } finally {
    hideLoading();
  }
}

async function fetchMe() {
  const t = getToken();
  if (!t) return;
  try {
    const res = await fetch(`${API_BASE}/auth/me`, { headers: getAuthHeaders() });
    if (!res.ok) throw new Error("Sessão inválida");
    const data = await res.json();
    
    const emailEl = document.getElementById("user-email");
    if(emailEl) emailEl.textContent = `${data.full_name || "Usuário"} (${data.email})`;
    
    updateTopbarUser(data);
    const credits = extractCredits(data);
    setCreditsUI(credits);
    showSection("section-dashboard");
    if (loadHistoricoFn) loadHistoricoFn();
  } catch(e) {
    setToken(null);
    updateTopbarUser(null);
    showSection("section-landing"); // ou auth
  }
}

document.addEventListener("DOMContentLoaded", () => {
  // Navigation
  const btnNavLogin = document.getElementById("btn-nav-login");
  const btnCtaStart = document.getElementById("btn-cta-start");
  const btnCtaLogin = document.getElementById("btn-cta-login");
  const btnPromoStart = document.getElementById("btn-promo-start");
  const btnLogout = document.getElementById("btn-logout");
  const btnLogoutTopbar = document.getElementById("btn-logout-topbar");

  // Auth switchers
  const cardLogin = document.getElementById("card-login");
  const cardRegister = document.getElementById("card-register");
  const cardForgot = document.getElementById("card-forgot-password");

  const btnGoRegister = document.getElementById("btn-go-register");
  const btnReturnLogin = document.getElementById("btn-return-to-login");
  const btnGoForgot = document.getElementById("btn-go-forgot-password");
  const btnBackFromForgot = document.getElementById("btn-back-from-forgot");

  // Forms
  const formLogin = document.getElementById("form-login");
  const formRegister = document.getElementById("form-register");
  const formForgot = document.getElementById("form-forgot-password");
  const formCorrigir = document.getElementById("form-corrigir");
  const formCorrigirArquivo = document.getElementById("form-corrigir-arquivo");

  const msgLogin = document.getElementById("msg-login");
  const msgRegister = document.getElementById("msg-register");
  const msgForgot = document.getElementById("msg-forgot");
  const msgCorrigir = document.getElementById("msg-corrigir");
  const msgCorrigirArquivo = document.getElementById("msg-corrigir-arquivo");

  updateTopbarUser = (data) => {
    const navAuth = document.getElementById("nav-auth");
    const navLogged = document.getElementById("nav-logged");
    const nameEl = document.getElementById("topbar-user-name");
    
    if (data) {
      navAuth.classList.add("hidden");
      navLogged.classList.remove("hidden");
      if(nameEl) nameEl.textContent = data.full_name?.split(" ")[0] || "Aluno";
    } else {
      navAuth.classList.remove("hidden");
      navLogged.classList.add("hidden");
    }
  };

  function goToAuth(mode='login') {
    showSection("section-auth");
    cardLogin.classList.remove("hidden");
    cardRegister.classList.add("hidden");
    cardForgot.classList.add("hidden");
    if(mode==='register') {
      cardLogin.classList.add("hidden");
      cardRegister.classList.remove("hidden");
    }
  }
  window.goToAuth = goToAuth;

  document.querySelectorAll("[data-buy-credits]").forEach(btn => {
    btn.addEventListener("click", (e) => {
      e.preventDefault();
      startCheckout();
    });
  });

  document.addEventListener("click", (e) => {
    const starBtn = e.target.closest(".star-btn");
    if (starBtn && starBtn.closest("[data-review-stars]")) {
      const starsContainer = starBtn.closest("[data-review-stars]");
      const value = Number(starBtn.dataset.star || 0);
      renderStars(starsContainer, value);
      return;
    }

    const toggleBtn = e.target.closest("[data-review-toggle]");
    if (toggleBtn) {
      const widget = toggleBtn.closest("[data-review-widget]");
      if (!widget) return;
      const isOpen = widget.classList.toggle("open");
      setReviewToggleLabel(widget, isOpen);
      return;
    }

    const saveBtn = e.target.closest("[data-review-save]");
    if (saveBtn) {
      const widget = saveBtn.closest("[data-review-widget]");
      submitReview(widget);
    }
  });

  // Listeners Nav
  if(btnNavLogin) btnNavLogin.addEventListener("click", () => goToAuth('login'));
  if(btnCtaStart) btnCtaStart.addEventListener("click", () => goToAuth('register'));
  if(btnCtaLogin) btnCtaLogin.addEventListener("click", () => goToAuth('login'));
  if(btnPromoStart) btnPromoStart.addEventListener("click", () => goToAuth('register'));
  
  if(btnLogout) btnLogout.addEventListener("click", () => { setToken(null); updateTopbarUser(null); showSection("section-landing"); });
  if(btnLogoutTopbar) btnLogoutTopbar.addEventListener("click", () => { setToken(null); updateTopbarUser(null); showSection("section-landing"); });

  // Auth Internal
  if(btnGoRegister) btnGoRegister.addEventListener("click", (e)=>{ e.preventDefault(); cardLogin.classList.add("hidden"); cardRegister.classList.remove("hidden"); });
  if(btnReturnLogin) btnReturnLogin.addEventListener("click", (e)=>{ e.preventDefault(); cardRegister.classList.add("hidden"); cardLogin.classList.remove("hidden"); });
  if(btnGoForgot) btnGoForgot.addEventListener("click", (e)=>{ e.preventDefault(); cardLogin.classList.add("hidden"); cardForgot.classList.remove("hidden"); });
  if(btnBackFromForgot) btnBackFromForgot.addEventListener("click", (e)=>{ e.preventDefault(); cardForgot.classList.add("hidden"); cardLogin.classList.remove("hidden"); });

  // Submits
  formLogin?.addEventListener("submit", async (e) => {
    e.preventDefault();
    msgLogin.textContent = "";
    showLoading("Entrando...");
    try {
      const res = await fetch(`${API_BASE}/auth/login`, {
        method: "POST", headers: getAuthHeaders(),
        body: JSON.stringify({ email: formLogin.email.value, password: formLogin.password.value })
      });
      if(!res.ok) throw new Error("E-mail ou senha incorretos");
      const d = await res.json();
      setToken(d.access_token);
      await fetchMe();
    } catch(err) {
      msgLogin.textContent = err.message;
      msgLogin.className = "form-message error";
    } finally { hideLoading(); }
  });

  formRegister?.addEventListener("submit", async (e) => {
    e.preventDefault();
    msgRegister.textContent = "";
    showLoading("Criando conta...");
    try {
      const res = await fetch(`${API_BASE}/auth/register`, {
        method: "POST", headers: getAuthHeaders(),
        body: JSON.stringify({ full_name: formRegister.full_name.value, email: formRegister.email.value, password: formRegister.password.value })
      });
      if(!res.ok) throw new Error("Erro ao criar conta");
      msgRegister.textContent = "Conta criada! Verifique seu e-mail.";
      msgRegister.className = "form-message success";
      formRegister.reset();
    } catch(err) {
      msgRegister.textContent = err.message;
      msgRegister.className = "form-message error";
    } finally { hideLoading(); }
  });

  formForgot?.addEventListener("submit", async (e) => {
    e.preventDefault();
    msgForgot.textContent = "";
    showLoading("Enviando...");
    try {
      const res = await fetch(`${API_BASE}/auth/forgot-password`, {
        method: "POST", headers: getAuthHeaders(), body: JSON.stringify({ email: formForgot.email.value })
      });
      if(!res.ok) throw new Error("Erro");
      msgForgot.textContent = "Link enviado!";
      msgForgot.className = "form-message success";
    } catch(err) {
      msgForgot.textContent = "Erro ao enviar.";
      msgForgot.className = "form-message error";
    } finally { hideLoading(); }
  });

  // Correction Logic
  const tabs = document.querySelectorAll(".switch-tab");
  tabs.forEach(t => {
    t.addEventListener("click", () => {
      tabs.forEach(x => x.classList.remove("active"));
      t.classList.add("active");
      const target = t.dataset.target;
      document.getElementById("panel-arquivo").classList.toggle("active", target === "arquivo");
      document.getElementById("panel-texto").classList.toggle("active", target === "texto");
    });
  });

  async function sendCorrection(url, body, msgEl, isFile=false) {
    showLoading("Corrigindo redação...");
    msgEl.textContent = "";
    try {
      const token = getToken();
      const headers = isFile ? { Authorization: `Bearer ${token}` } : getAuthHeaders();
      const res = await fetch(`${API_BASE}${url}`, { method: "POST", headers, body });
      let d = null;
      try { d = await res.json(); } catch (err) { d = null; }
      if(!res.ok) {
        const serverMsg = d?.detail || d?.message || d?.error || "Falha na correção.";
        throw new Error(serverMsg);
      }
      lastEssayId = d?.essay_id || d?.id || d?.resultado?.essay_id || d?.resultado?.id || null;
      lastReview = d?.review || d?.resultado?.review || null;
      const resultado = d?.resultado || d;
      renderResultado(resultado);
      updateResultadoReview(lastEssayId, lastReview);
      const credits = extractCredits(d);
      if (credits !== null) setCreditsUI(credits);
      loadHistoricoFn();
      msgEl.textContent = "Corrigido com sucesso!";
      msgEl.className = "form-message success";
      document.getElementById("resultado-wrapper").scrollIntoView({behavior:"smooth"});
    } catch(err) {
      msgEl.textContent = err.message;
      msgEl.className = "form-message error";
    } finally { hideLoading(); }
  }

  formCorrigir?.addEventListener("submit", (e) => {
    e.preventDefault();
    sendCorrection("/app/enem/corrigir-texto", JSON.stringify({ tema: formCorrigir.tema.value, texto: formCorrigir.texto.value }), msgCorrigir);
  });

  formCorrigirArquivo?.addEventListener("submit", (e) => {
    e.preventDefault();
    const fd = new FormData(formCorrigirArquivo);
    fd.append("tema", formCorrigirArquivo.tema_arquivo.value);
    sendCorrection("/app/enem/corrigir-arquivo", fd, msgCorrigirArquivo, true);
  });

  function renderResultado(res) {
    const el = document.getElementById("resultado-wrapper");
    if(!res || !el) return;
    const comps = (res.competencias || []).map(c => `
      <div class="competencia-card">
        <div class="competencia-header">
           <span>Competência ${c.id}</span>
           <span class="competencia-badge">${c.nota} / 200</span>
        </div>
        <div style="font-size:0.9rem; color:#475569;">${marked.parse(c.feedback||"")}</div>
      </div>
    `).join("");
    el.innerHTML = `
      <div style="text-align:center; margin-bottom:1.5rem;">
        <span style="font-size:0.9rem; color:#64748b;">NOTA FINAL</span><br>
        <span class="resultado-score-pill">${res.nota_final}</span>
      </div>
      <div style="margin-bottom:1.5rem; line-height:1.6;">${marked.parse(res.analise_geral||"")}</div>
      <h4>Detalhamento por competência</h4>
      ${comps}
    `;
  }

  loadHistoricoFn = async () => {
    try {
      const res = await fetch(`${API_BASE}/app/enem/historico`, { headers: getAuthHeaders() });
      if(!res.ok) return;
      const data = await res.json();
      const items = (data.historico || []);
      
      // Update Resumo
      const stats = data.stats || {};
      const resumo = document.getElementById("evolucao-resumo");
      if(resumo) {
        resumo.innerHTML = `
          <div style="text-align:center;">
             <small style="color:#64748b; font-weight:700;">MÉDIA</small>
             <div style="font-size:1.4rem; font-weight:800; color:var(--brand);">${stats.media_nota_final?.toFixed(0)||"0"}</div>
          </div>
          <div style="width:1px; background:#e2e8f0;"></div>
          <div style="text-align:center;">
             <small style="color:#64748b; font-weight:700;">MELHOR</small>
             <div style="font-size:1.4rem; font-weight:800; color:#22c55e;">${stats.melhor_nota||"0"}</div>
          </div>
        `;
      }
      
      const list = document.getElementById("historico-list");
      if(list) {
        if(!items.length) list.innerHTML = "<p style='color:#94a3b8; text-align:center; padding:1rem;'>Nenhuma redação ainda.</p>";
        else {
          list.innerHTML = items.map(i => {
            const review = i.review || null;
            return `
              <div class="historico-item" data-essay-id="${i.id}">
                <div class="historico-main">
                  <div>
                    <strong style="display:block; font-size:0.9rem; color:#334155;">${i.tema || "Sem tema"}</strong>
                    <small style="color:#94a3b8;">${new Date(i.created_at).toLocaleDateString()}</small>
                  </div>
                  <span style="font-weight:800; color:var(--brand); font-size:1rem;">${i.nota_final||"-"}</span>
                </div>
                <div class="review-widget" data-review-widget data-essay-id="${i.id}" data-initial-stars="${review?.stars || 0}" data-initial-comment="${encodeAttr(review?.comment || "")}" data-initial-created-at="${encodeAttr(review?.created_at || "")}" data-initial-updated-at="${encodeAttr(review?.updated_at || "")}">
                  <div class="review-summary" data-review-summary>
                    <span class="review-summary-stars" data-review-summary-stars>Sem avaliação</span>
                    <span class="review-badge hidden" data-review-badge></span>
                    <button type="button" class="link-btn review-toggle" data-review-toggle>Avaliar</button>
                  </div>
                  <div class="review-body">
                    <div class="review-header">Avalie esta correção</div>
                    <div class="review-row">
                      <div class="review-stars" data-review-stars></div>
                      <button type="button" class="duo-btn btn-secondary small" data-review-save>Salvar avaliação</button>
                    </div>
                    <textarea class="review-input" rows="2" placeholder="Comentário (opcional)" data-review-comment></textarea>
                    <p class="form-message" data-review-msg></p>
                  </div>
                </div>
              </div>
            `;
          }).join("");
          hydrateReviewWidgets(list);
        }
      }
      
      // Update Chart
      const canvas = document.getElementById("evolucaoChart");
      if(canvas && typeof Chart !== "undefined" && items.length > 0) {
        const sorted = items.filter(x => typeof x.nota_final==='number').sort((a,b)=>new Date(a.created_at)-new Date(b.created_at));
        const labels = sorted.map(x => new Date(x.created_at).toLocaleDateString(undefined, {day:'2-digit',month:'2-digit'}));
        const values = sorted.map(x => x.nota_final);

        if(chartInstance) {
          chartInstance.data.labels = labels;
          chartInstance.data.datasets[0].data = values;
          chartInstance.update();
        } else {
          chartInstance = new Chart(canvas.getContext("2d"), {
             type: 'line',
             data: {
               labels,
               datasets: [{
                 label: 'Nota',
                 data: values,
                 borderColor: '#2563eb',
                 backgroundColor: 'rgba(37, 99, 235, 0.1)',
                 tension: 0.3,
                 fill: true
               }]
             },
             options: {
               responsive: true,
               maintainAspectRatio: false,
               plugins: { legend: {display:false} },
               scales: { y: { min: 0, max: 1000 } }
             }
          });
        }
      }

    } catch(e){console.error(e);}
  };

  if(getToken()) fetchMe();
});
