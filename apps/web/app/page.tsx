"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { SiteFooter, SiteNav } from "./components/SiteChrome";
import { detectPlatform, latestInstallers, type Installers, type Platform } from "./downloads";
import { getMe } from "./lib";

export default function Home() {
  const [signedIn, setSignedIn] = useState(false);
  const [os, setOs] = useState<Platform>("other");
  const [installers, setInstallers] = useState<Installers>({ page: "/download" });

  useEffect(() => {
    setOs(detectPlatform());
    void latestInstallers().then(setInstallers);
    getMe().then((me) => setSignedIn(Boolean(me)));
  }, []);

  const downloadHref = os === "windows" ? installers.windows || "/download" : os === "mac" ? installers.mac || "/download" : "/download";
  const downloadLabel = os === "windows" ? "Download for Windows" : os === "mac" ? "Download for Mac" : "Download the app";

  return (
    <div className="site">
      <SiteNav signedIn={signedIn} />
      <main>
        <section className="site-hero">
          <p className="site-kicker">Personal assistant</p>
          <h1>
            Work in the browser.
            <br />
            Or on the computer in front of you.
          </h1>
          <p className="site-lead">
            Lumio is your private chief of staff for mail, calendar, files, and the live screen. Sign in, open the web app,
            or install on Mac and Windows — same assistant, the computer you choose.
          </p>
          <div className="site-cta">
            {signedIn ? (
              <Link className="site-btn primary" href="/chat">
                Open Lumio
              </Link>
            ) : (
              <>
                <Link className="site-btn primary" href="/login">
                  Sign in
                </Link>
                <Link className="site-btn" href="/chat">
                  Use the web app
                </Link>
              </>
            )}
            <a className="site-btn ghost" href={downloadHref}>
              {downloadLabel}
            </a>
          </div>
          <p className="site-hero-note">Google or Microsoft · Web, macOS, Windows · Your accounts stay yours</p>
          <div className="site-stage" aria-hidden>
            <div className="site-window">
              <div className="site-window-bar">
                <span />
                <span />
                <span />
                <em>Lumio</em>
              </div>
              <div className="site-window-body">
                <aside>
                  <b>Chief</b>
                  <small>Inbox · Calendar · Computer</small>
                </aside>
                <div>
                  <p className="you">Book Mumbai to Goa, 22 Nov, one way.</p>
                  <p className="bot">On it. Opening Booking.com in Chrome on this computer.</p>
                  <div className="site-screen-chip">this computer · Chrome</div>
                </div>
              </div>
            </div>
          </div>
        </section>

        <section className="site-paths" id="start">
          <h2>Start the way you work</h2>
          <div className="site-path-grid">
            <Link className="site-path" href="/login">
              <strong>Sign in</strong>
              <span>Google or Microsoft. One private workspace for mail, calendar, and connected apps.</span>
              <em>Continue →</em>
            </Link>
            <Link className="site-path" href="/chat">
              <strong>Use the web app</strong>
              <span>Open Lumio in the browser. The assistant uses a cloud computer with a live screen.</span>
              <em>Open in browser →</em>
            </Link>
            <Link className="site-path featured" href="/download">
              <strong>Download the app</strong>
              <span>Mac and Windows. This computer — files, apps, Chrome, and taught skills.</span>
              <em>Get Mac or Windows →</em>
            </Link>
          </div>
        </section>

        <section className="site-product" id="product">
          <h2>One assistant. Every surface.</h2>
          <div className="site-feature-grid">
            <article>
              <strong>A computer that works with you</strong>
              <p>Watch the live screen, take control, or hand a booking to Lumio. On the web it is a secure cloud desktop. In the app it is this computer — Finder, files, apps, and Chrome.</p>
            </article>
            <article>
              <strong>Mail and calendar, in the conversation</strong>
              <p>Read, draft, and send with confirmation. Schedule the next step. Inbox cards stay in one place.</p>
            </article>
            <article>
              <strong>Teach a task once</strong>
              <p>Show Lumio on this computer: browse files, use apps, click through a site. Narrate if you want. It writes a skill you can run again.</p>
            </article>
            <article>
              <strong>Yours, not a shared inbox</strong>
              <p>Sign in with the account you already use. Connectors, files, and the desktop jail stay in your workspace.</p>
            </article>
          </div>
        </section>
      </main>
      <SiteFooter />
    </div>
  );
}
