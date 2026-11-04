const $ = (id) => document.getElementById(id);
const setStatus = (id, text) => { $(id).textContent = text; };

const COMPLETED_MANUSCRIPT_KEY = "creatorpay:completed-manuscript:v1";
const COMPLETED_PAGE_COUNT_KEY = "creatorpay:completed-page-count:v1";
let completedPageCount = 5;
try {
  const saved = Number(localStorage.getItem(COMPLETED_PAGE_COUNT_KEY));
  if (Number.isInteger(saved) && saved >= 1 && saved <= 20) completedPageCount = saved;
} catch (_) {}
let completedManuscript = "";
try {
  completedManuscript = localStorage.getItem(COMPLETED_MANUSCRIPT_KEY) || "";
} catch (_) {}


async function api(path, body) {
  const response = await fetch(path, {
    method: body ? "POST" : "GET",
    headers: body ? {"Content-Type":"application/json"} : {},
    body: body ? JSON.stringify(body) : undefined
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || `Request failed (${response.status})`);
  return data;
}

async function loadHealth() {
  try {
    const h = await api("/api/health");
    setStatus("ai-status", h.ai_configured
      ? `AI connected · ${h.paypal_mode} PayPal mode`
      : "AI key not configured yet. Product drafting is ready once Gemini is connected.");
  } catch (e) { setStatus("ai-status", e.message); }
}


const CHECKPOINT_KEY = "creatorpay:generation-checkpoint:v1";

function getCheckpoint() {
  try {
    return JSON.parse(localStorage.getItem(CHECKPOINT_KEY) || "null");
  } catch (_) { return null; }
}

function saveCheckpoint(data) {
  try {
    localStorage.setItem(CHECKPOINT_KEY, JSON.stringify(data));
    $("resume-generation").hidden = false;
    return true;
  } catch (_) {
    setStatus("ai-status", "Could not save recovery checkpoint. Copy your visible draft.");
    return false;
  }
}

function updateResumeButton() {
  $("resume-generation").hidden = !getCheckpoint();
}

function stripChapterHeading(value) {
  return String(value || "").trim()
    .replace(/^#{1,6}\s+[^\n]*(?:\r?\n|$)\s*/, "").trim();
}

function parseOutline(outline) {
  const re = /^#{2,4}\s*(Chapter\s+\d+\s*[:—-]?.*)$/gim;
  const matches = [...outline.matchAll(re)];
  if (!matches.length) return null;

  const last = matches[matches.length - 1];
  const tail = /^#{1,4}\s*(?:Conclusion|Launch Action Checklist|Appendix)\b/im
    .exec(outline.slice(last.index + last[0].length));
  const tailStart = tail
    ? last.index + last[0].length + tail.index
    : outline.length;

  return {
    prefix: outline.slice(0, matches[0].index).trim(),
    ending: outline.slice(tailStart).trim(),
    chapters: matches.map((m, i) => {
      const from = m.index + m[0].length;
      const to = i + 1 < matches.length ? matches[i + 1].index : tailStart;
      return {title: m[1].trim(), outline: outline.slice(from, to).trim()};
    })
  };
}

async function runGeneration(resume) {
  const generate = $("generate");
  const resumeButton = $("resume-generation");
  generate.disabled = true;
  resumeButton.disabled = true;

  try {
    let cp;

    if (resume) {
      cp = getCheckpoint();
      if (!cp || !Array.isArray(cp.chapters) ||
          !Number.isInteger(cp.nextIndex)) {
        throw new Error("No valid generation checkpoint was found.");
      }

      completedPageCount = Number(cp.page_count) || 5;
      $("idea").value = cp.idea;
      $("audience").value = cp.audience;
      $("type").value = cp.product_type;
      $("title").value = cp.title || "";
      $("description").value = cp.description || "";
      $("price").value = cp.price || "";

      if (!saveCheckpoint(cp)) throw new Error("Could not update checkpoint.");
    } else {
      const idea = $("idea").value.trim();
      if (!idea) {
        setStatus("ai-status", "Enter your product idea first.");
        return;
      }

      completedManuscript = "";
      try { localStorage.removeItem(COMPLETED_MANUSCRIPT_KEY); } catch (_) {}
      $("draft").value = "Generation in progress… Please wait until it is complete.";
      setStatus("ai-status", "Creating your product…");

      const result = await api("/api/ai/generate", {
        idea,
        audience: $("audience").value,
        product_type: $("type").value
      });
      completedPageCount = Number(result.page_count) || 5;

      if (result.title) $("title").value = result.title;
      if (result.short_description) $("description").value = result.short_description;
      if (result.suggested_price) $("price").value = result.suggested_price;

      const outline = result.draft || "";
      const parsed = parseOutline(outline);

      if (!parsed) {
        throw new Error("The AI returned an incomplete product plan.");
      }

      cp = {
        idea,
        audience: $("audience").value,
        product_type: $("type").value,
        title: $("title").value,
        description: $("description").value,
        price: $("price").value,
        page_count: completedPageCount,
        chapters: parsed.chapters,
        ending: parsed.ending,
        nextIndex: 0,
        manuscript: parsed.prefix ? parsed.prefix + "\n\n" : ""
      };

      if (!saveCheckpoint(cp)) {
        throw new Error("Plan created, but recovery storage failed.");
      }

      $("draft").value = `Generation in progress… Chapter ${cp.nextIndex} of ${cp.chapters.length} completed.`;
      window.saveCreatorPayDraft?.();
    }

    for (let i = cp.nextIndex; i < cp.chapters.length; i++) {
      const chapter = cp.chapters[i];
      setStatus("ai-status", `Writing chapter ${i + 1} of ${cp.chapters.length}…`);

      const result = await api("/api/ai/expand-chapter", {
        idea: cp.idea,
        audience: cp.audience,
        product_type: cp.product_type,
        chapter_title: chapter.title,
        chapter_outline: chapter.outline || chapter.title,
        page_count: cp.page_count || 5,
        chapter_count: cp.chapters.length
      });

      const content = stripChapterHeading(result.chapter);
      if (!content) throw new Error(`Chapter ${i + 1} returned empty content.`);

      cp.manuscript += `## ${chapter.title}\n\n${content}\n\n`;
      cp.nextIndex = i + 1;
      $("draft").value = `Generation in progress… Chapter ${cp.nextIndex} of ${cp.chapters.length} completed.`;
      window.saveCreatorPayDraft?.();

      if (!saveCheckpoint(cp)) {
        throw new Error("Chapter generated, but checkpoint saving failed.");
      }
    }

    if (cp.ending) cp.manuscript = cp.manuscript.trim() + "\n\n" + cp.ending + "\n";
    completedManuscript = cp.manuscript.trim();
    try {
      localStorage.setItem(COMPLETED_MANUSCRIPT_KEY, completedManuscript);
      localStorage.setItem(COMPLETED_PAGE_COUNT_KEY, String(cp.page_count || 5));
    } catch (_) {}

    $("draft").value = "All complete! Your content is ready. You can now save it.";
    window.saveCreatorPayDraft?.();

    localStorage.removeItem(CHECKPOINT_KEY);
    updateResumeButton();
    setStatus("ai-status", "All complete! Your content is ready to save.");
  } catch (e) {
    const checkpoint = getCheckpoint();
    updateResumeButton();

    if (checkpoint) {
      $("draft").value = "Generation paused. Select Resume generation to continue.";
      setStatus("ai-status",
        "Generation failed. Please try again. Your completed chapters are saved; select Resume generation to continue.");
    } else {
      $("draft").value = "";
      setStatus("ai-status", "Generation failed. Please try again.");
    }

    console.error("CreatorPay generation failed:", e);
  } finally {
    generate.disabled = false;
    resumeButton.disabled = false;
    updateResumeButton();
  }
}

$("generate").addEventListener("click", () => runGeneration(false));
$("resume-generation").addEventListener("click", () => runGeneration(true));
updateResumeButton(); // Restore checkpoint visibility on page load
if (completedManuscript) {
  $("draft").value = "All complete! Your content is ready. You can now save it.";
} else if (getCheckpoint()) {
  $("draft").value = "Generation paused. Select Resume generation to continue.";
}
$("save").addEventListener("click", async () => {
  const title = $("title").value.trim();
  const description = $("description").value.trim();
  const content = completedManuscript.trim();
  if (!content) {
    setStatus("save-status", "Finish generating your content before saving the product.");
    return;
  }
  const price = $("price").value;
  $("save").disabled = true;
  try {
    await api("/api/products", {
      title, description, content, price,
      page_count: completedPageCount
    });
    completedManuscript = "";
    try {
      localStorage.removeItem(COMPLETED_MANUSCRIPT_KEY);
      localStorage.removeItem(COMPLETED_PAGE_COUNT_KEY);
    } catch (_) {}
    completedPageCount = 5;

    // Remove all saved generation data.
    try {
      localStorage.removeItem(COMPLETED_MANUSCRIPT_KEY);
      localStorage.removeItem(COMPLETED_PAGE_COUNT_KEY);
      localStorage.removeItem(CHECKPOINT_KEY);
    } catch (_) {}

    // Empty every product creation field after successful saving.
    $("idea").value = "";
    $("audience").value = "";
    $("type").selectedIndex = 0;
    $("title").value = "";
    $("description").value = "";
    $("price").value = "";
    $("draft").value = "";
    $("generation-hint").textContent = "";

    updateResumeButton();
    window.saveCreatorPayDraft?.();

    setStatus("ai-status", "Ready to create another product.");
    setStatus("save-status", "Content saved successfully.");
    await loadProducts();
  } catch (e) {
    setStatus("save-status", "Content not saved. Try again.");
  }
  finally { $("save").disabled = false; }
});

async function loadProducts() {
  try {
    const products = await api("/api/products");
    const root = $("products");
    root.replaceChildren();
    if (!products.length) {
      root.innerHTML = '<p class="hint">No products saved yet. Create your first product above.</p>';
      return;
    }
    const seenProducts = new Set();
    products.forEach(p => {
      const duplicateKey = JSON.stringify(
        [p.title, p.description, p.price, p.currency]
          .map(value => String(value ?? "").trim().toLowerCase())
      );
      if (seenProducts.has(duplicateKey)) return;
      seenProducts.add(duplicateKey);

      const card = document.createElement("article");
      card.className = "product";
      const heading = document.createElement("h3");
      heading.textContent = p.title;
      const desc = document.createElement("p");
      desc.textContent = p.description;
      const price = document.createElement("div");
      price.className = "price";
      price.textContent = new Intl.NumberFormat("en-US",{style:"currency",currency:p.currency}).format(Number(p.price));
      const buy = document.createElement("button");
      buy.className = "secondary";

      const alreadyPurchased = getSavedPurchases()
        .some(record => record.product_id === p.id);

      if (alreadyPurchased) {
        buy.textContent = "Purchased";
        buy.disabled = true;
        buy.classList.add("purchased");
      } else {
        buy.textContent = "Pay with PayPal (Sandbox)";
        buy.addEventListener("click", () => checkout(p.id, buy));
      }

      card.append(heading, desc, price, buy);
      root.append(card);
    });
  } catch (e) { $("products").textContent = e.message; }
}


const PURCHASES_KEY = "creatorpay:purchases:v1";

function getSavedPurchases() {
  try {
    const records = JSON.parse(localStorage.getItem(PURCHASES_KEY) || "[]");
    return Array.isArray(records)
      ? records.filter(x =>
          x && typeof x.order_id === "string" &&
          typeof x.delivery_token === "string" &&
          typeof x.product_id === "string"
        )
      : [];
  } catch (_) {
    return [];
  }
}

function rememberPurchase(record) {
  const records = getSavedPurchases();
  const existing = records.findIndex(x => x.order_id === record.order_id);

  if (existing >= 0) records[existing] = record;
  else records.unshift(record);

  localStorage.setItem(PURCHASES_KEY, JSON.stringify(records));
}


async function downloadPurchasedProduct(record, format, button) {
  const original = button.textContent;
  button.disabled = true;
  button.textContent = "Preparing download…";

  try {
    const response = await fetch("/api/orders/download", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({
        order_id: record.order_id,
        delivery_token: record.delivery_token,
        format
      })
    });

    if (!response.ok) {
      const data = await response.json().catch(() => ({}));
      throw new Error(data.error || `Download failed (${response.status})`);
    }

    const blob = await response.blob();
    const disposition = response.headers.get("Content-Disposition") || "";
    const match = disposition.match(/filename="([^"]+)"/);
    const filename = match ? match[1] :
      `creatorpay-purchase.${format}`;

    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = filename;
    document.body.append(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
    button.textContent = "Downloaded";
  } catch (err) {
    alert(err.message || "Download failed.");
    button.textContent = original;
  } finally {
    button.disabled = false;
  }
}

async function loadPurchases() {
  const root = document.getElementById("purchases");
  if (!root) return;

  const records = getSavedPurchases();
  root.replaceChildren();

  if (!records.length) {
    root.textContent = "Your verified purchases will appear here after payment.";
    return;
  }

  for (const record of records) {
    const card = document.createElement("article");
    card.className = "product";

    const heading = document.createElement("h3");
    heading.textContent = "Verifying purchase…";

    const status = document.createElement("p");
    status.className = "hint";
    status.textContent = "Checking payment authorization with CreatorPay.";

    card.append(heading, status);
    root.append(card);

    try {
      const purchased = await api("/api/orders/deliver", {
        order_id: record.order_id,
        delivery_token: record.delivery_token
      });

      heading.textContent = purchased.title;
      status.textContent = "Payment verified · Product available";

      const details = document.createElement("details");
      const summary = document.createElement("summary");
      summary.textContent = "View purchased product";

      const content = document.createElement("pre");
      content.textContent = purchased.content;
      content.style.whiteSpace = "pre-wrap";
      content.style.overflowWrap = "anywhere";

      details.replaceChildren(summary, content);
      card.append(details);

      const downloadLabel = document.createElement("p");
      downloadLabel.className = "hint";
      downloadLabel.textContent = "Choose your download format:";

      const actions = document.createElement("div");
      actions.className = "purchase-download-actions";

      const pdfButton = document.createElement("button");
      pdfButton.type = "button";
      pdfButton.textContent = "Download PDF";
      pdfButton.addEventListener("click", () =>
        downloadPurchasedProduct(record, "pdf", pdfButton)
      );

      const txtButton = document.createElement("button");
      txtButton.type = "button";
      txtButton.textContent = "Download text (.txt)";
      txtButton.addEventListener("click", () =>
        downloadPurchasedProduct(record, "txt", txtButton)
      );

      actions.append(pdfButton, txtButton);
      card.append(downloadLabel, actions);
    } catch (err) {
      heading.textContent = "Purchase could not be verified";
      status.textContent =
        "Your saved purchase reference could not currently be verified. " +
        "It has been retained so you can try again later.";
    }
  }
}

async function checkout(productId, button) {
  const originalText = button.textContent;
  button.disabled = true;
  button.textContent = "Preparing PayPal checkout…";

  let container;

  try {
    const cfg = await api("/api/config");
    if (!cfg.paypal_client_id) {
      throw new Error("PayPal Client ID is missing.");
    }

    // Load the PayPal v6 Sandbox SDK only.
    if (!window.paypal || typeof window.paypal.createInstance !== "function") {
      await new Promise((resolve, reject) => {
        const script = document.createElement("script");
        script.src = "https://www.sandbox.paypal.com/web-sdk/v6/core";
        script.async = true;
        script.onload = resolve;
        script.onerror = () =>
          reject(new Error("Could not load PayPal v6 Sandbox SDK."));
        document.head.append(script);
      });
    }

    if (!window.paypal || typeof window.paypal.createInstance !== "function") {
      throw new Error("PayPal v6 SDK did not initialize correctly. Refresh the page.");
    }

    const sdk = await window.paypal.createInstance({
      clientId: cfg.paypal_client_id,
      components: ["paypal-payments"],
      pageType: "product-details",
      locale: "en-US"
    });

    const eligible = await sdk.findEligibleMethods({
      currencyCode: "USD"
    });

    if (!eligible.isEligible("paypal")) {
      throw new Error("PayPal checkout is not eligible for this browser or account.");
    }

    container = document.createElement("div");
    container.className = "checkout-status";
    container.setAttribute("role", "status");
    button.after(container);

    const createOrder = async () => {
      const order = await api("/api/paypal/create-order", {
        product_id: productId
      });

      if (!order || !order.id) {
        throw new Error("Our server did not return a PayPal order ID.");
      }

      console.info("[CreatorPay] PayPal order created:", order.id);
      return { orderId: order.id };
    };

    const session = sdk.createPayPalOneTimePaymentSession({
      onApprove: async (data) => {
        container.textContent =
          "PayPal approval received. Verifying payment securely…";

        console.info("[CreatorPay] PayPal v6 approval callback:", data);

        try {
          const paypalOrderId = data && data.orderId;
          if (!paypalOrderId) {
            throw new Error("PayPal approval did not include an order ID.");
          }

          const result = await api("/api/paypal/capture-order", {
            order_id: paypalOrderId
          });

          if (result.status !== "COMPLETED" || !result.delivery_token) {
            throw new Error("PayPal did not confirm payment. Product not delivered.");
          }

          const purchased = await api("/api/orders/deliver", {
            order_id: paypalOrderId,
            delivery_token: result.delivery_token
          });

          rememberPurchase({
            order_id: paypalOrderId,
            product_id: result.product_id,
            delivery_token: result.delivery_token,
            saved_at: new Date().toISOString()
          });

          const details = document.createElement("details");
          const heading = document.createElement("summary");
          heading.textContent = "Your purchased product: " + purchased.title;

          const content = document.createElement("pre");
          content.textContent = purchased.content;
          content.style.whiteSpace = "pre-wrap";
          content.style.overflowWrap = "anywhere";

          details.replaceChildren(heading, content);
          container.replaceChildren(details);
          button.hidden = true;

          alert("Payment verified. Your product is ready.");
          await loadPurchases();
          await loadProducts();
        } catch (err) {
          console.error("[CreatorPay] Approval or delivery failed:", err);
          container.textContent =
            "Payment verification or delivery failed: " +
            (err.message || "Unknown error") +
            ". Your product has not been delivered. Check order status before retrying.";
          button.disabled = false;
          button.textContent = "Check payment status";
          alert(container.textContent);
        }
      },

      onCancel: () => {
        console.warn("[CreatorPay] Buyer cancelled PayPal v6 checkout.");
        container.textContent =
          "Checkout was cancelled. No product was delivered.";
        button.disabled = false;
        button.textContent = "Try PayPal checkout again";
      },

      onError: (err) => {
        console.error("[CreatorPay] PayPal v6 session error:", err);
        container.textContent =
          "PayPal checkout error: " +
          (err && err.message ? err.message : String(err || "Unknown error"));
        button.disabled = false;
        button.textContent = "Try PayPal checkout again";
      }
    });

    // Start only in response to the buyer's button click.
    button.textContent = "Opening PayPal…";
    await session.start(
      { presentationMode: "auto" },
      createOrder()
    );

  } catch (err) {
    console.error("[CreatorPay] v6 checkout setup/start failed:", err);

    if (container && !container.textContent) {
      container.textContent = err.message || "Could not start PayPal checkout.";
    }

    button.disabled = false;
    button.textContent = originalText;
    alert(err.message || "Could not prepare PayPal checkout.");
  }
}

loadHealth();
loadPurchases();
loadProducts();


// Restore and automatically save the creator's editor draft.
(() => {
  const storageKey = "creatorpay:editor-draft:v1";
  const fields = ["idea", "audience", "type", "title", "description", "price"];

  window.saveCreatorPayDraft = () => {
    try {
      const state = {};
      for (const id of fields) {
        const element = document.getElementById(id);
        if (element) state[id] = element.value;
      }
      localStorage.setItem(storageKey, JSON.stringify(state));
    } catch (error) {
      console.warn("CreatorPay draft autosave unavailable.");
    }
  };

  try {
    const saved = JSON.parse(localStorage.getItem(storageKey) || "{}");
    for (const id of fields) {
      const element = document.getElementById(id);
      if (element && typeof saved[id] === "string") {
        element.value = saved[id];
      }
    }
  } catch (error) {
    console.warn("Could not restore the previous CreatorPay draft.");
  }

  for (const id of fields) {
    const element = document.getElementById(id);
    if (element) {
      element.addEventListener("input", window.saveCreatorPayDraft);
      element.addEventListener("change", window.saveCreatorPayDraft);
    }
  }

  window.addEventListener("beforeunload", window.saveCreatorPayDraft);
})();
