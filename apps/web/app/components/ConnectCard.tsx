"use client";

export type ConnectCardView = {
  toolkit?: string;
  name?: string;
  logo?: string;
  description?: string;
  redirect_url?: string;
};

function asCards(value: unknown): ConnectCardView[] {
  if (Array.isArray(value)) return value.filter((row) => row && typeof row === "object") as ConnectCardView[];
  if (typeof value === "string") {
    try {
      return asCards(JSON.parse(value));
    } catch {
      return [];
    }
  }
  return [];
}

export function cardsFromMeta(meta: any): ConnectCardView[] {
  const out: ConnectCardView[] = [];
  const seen = new Set<string>();
  const add = (row: any) => {
    if (!row || typeof row !== "object") return;
    const url = String(row.redirect_url || row.redirectUrl || "").trim();
    if (!url || seen.has(url)) return;
    seen.add(url);
    out.push({
      toolkit: row.toolkit || "",
      name: row.name || row.toolkit || "Connect app",
      logo: row.logo || "",
      description: row.description || "",
      redirect_url: url,
    });
  };
  for (const row of asCards(meta?.connect_cards)) add(row);
  add(meta?.source?.snippet);
  add(meta?.source);
  return out;
}

function ConnectPanel({ card }: { card: ConnectCardView }) {
  const href = card.redirect_url || "";
  const title = card.name || card.toolkit || "Connect app";
  return (
    <div className="connect-card">
      <header className="connect-card-head">
        {card.logo ? <img className="connect-card-logo" src={card.logo} alt="" /> : <span className="connect-card-mark" />}
        <div className="connect-card-copy">
          <strong>{title}</strong>
          <p>{card.description || "Authorize this app in Composio to use it from chat."}</p>
        </div>
        {href ? (
          <a className="btn" href={href} target="_blank" rel="noreferrer">
            Connect
          </a>
        ) : null}
      </header>
      {href ? (
        <details className="connect-card-embed">
          <summary>Composio authorization</summary>
          <div className="browser-frame connect-card-browser">
            <div className="browser-chrome">
              <span className="browser-dots" aria-hidden>
                <i />
                <i />
                <i />
              </span>
              <div className="browser-url">{href}</div>
              <a className="browser-open" href={href} target="_blank" rel="noreferrer">
                Open tab
              </a>
            </div>
            <div className="browser-stage">
              <iframe
                className="browser-iframe"
                title={title}
                src={href}
                allow="clipboard-write"
                sandbox="allow-scripts allow-same-origin allow-forms allow-popups allow-popups-to-escape-sandbox"
              />
              <a className="connect-card-fallback" href={href} target="_blank" rel="noreferrer">
                If this page is blank, continue in Composio
              </a>
            </div>
          </div>
        </details>
      ) : null}
    </div>
  );
}

export function ConnectCards({ cards, meta }: { cards?: ConnectCardView[]; meta?: any }) {
  const list = cardsFromMeta(meta || { connect_cards: cards });
  if (!list.length) return null;
  return (
    <div className="connect-card-list">
      {list.map((card, i) => (
        <ConnectPanel key={`${card.toolkit || card.redirect_url}-${i}`} card={card} />
      ))}
    </div>
  );
}
