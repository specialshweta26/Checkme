"use client";

import { useEffect, useState, type ReactNode } from "react";
import Link from "next/link";
import { SiteFooter, SiteNav } from "../components/SiteChrome";
import { detectPlatform, latestInstallers, type Installers, type Platform } from "../downloads";
import { getMe } from "../lib";

export default function Download() {
  const [signedIn, setSignedIn] = useState(false);
  const [os, setOs] = useState<Platform>("other");
  const [installers, setInstallers] = useState<Installers>({ page: "https://github.com/specialshweta26/Checkme/releases/latest" });

  useEffect(() => {
    setOs(detectPlatform());
    void latestInstallers().then(setInstallers);
    getMe().then((me) => setSignedIn(Boolean(me)));
  }, []);

  return (
    <div className="site">
      <SiteNav signedIn={signedIn} />
      <main className="site-download">
        <p className="site-kicker">Desktop</p>
        <h1>Lumio for Mac and Windows</h1>
        <p className="site-lead">
          The web app runs on a cloud computer. The desktop app uses Chrome, files, and teaching on this machine — so bookings,
          logins, and skills stay on your computer.
        </p>
        <div className="site-os-grid">
          <OsCard
            title="macOS"
            hint="Apple silicon and Intel. macOS 12+"
            href={installers.mac || installers.page}
            cta={installers.mac ? "Download for Mac" : "Get the Mac build"}
            highlight={os === "mac"}
            icon={<AppleIcon />}
          />
          <OsCard
            title="Windows"
            hint="Windows 10 and 11, 64-bit"
            href={installers.windows || installers.page}
            cta={installers.windows ? "Download for Windows" : "Get the Windows build"}
            highlight={os === "windows"}
            icon={<WindowsIcon />}
          />
        </div>
        <p className="site-download-note">
          Prefer not to install? <Link href="/chat">Use Lumio in the browser</Link>
          {" · "}
          <Link href="/login">Sign in</Link>
        </p>
      </main>
      <SiteFooter />
    </div>
  );
}

function OsCard({
  title,
  hint,
  href,
  cta,
  highlight,
  icon,
}: {
  title: string;
  hint: string;
  href: string;
  cta: string;
  highlight: boolean;
  icon: ReactNode;
}) {
  return (
    <a className={`site-os-card${highlight ? " recommended" : ""}`} href={href}>
      <span className="site-os-icon">{icon}</span>
      <strong>{title}</strong>
      <span>{hint}</span>
      <em>{highlight ? `Recommended · ${cta}` : cta}</em>
    </a>
  );
}

function AppleIcon() {
  return (
    <svg width="28" height="28" viewBox="0 0 24 24" fill="currentColor" aria-hidden>
      <path d="M16.4 12.6c0-2.3 1.9-3.4 2-3.5-1.1-1.6-2.8-1.8-3.4-1.8-1.4-.2-2.8.9-3.5.9s-1.8-0.8-3-.8c-1.5 0-3 .9-3.7 2.3-1.6 2.8-.4 6.9 1.1 9.2.8 1.1 1.7 2.3 2.9 2.3 1.1 0 1.6-.7 3-.7s1.8.7 3 .7 2-.1 2.9-2.3c.7-1.1 1-2.1 1-2.2-.1 0-2.3-.9-2.3-3.9zM14.6 5.8c.6-.8 1.1-1.8.9-2.8-0.9 0-2 .6-2.6 1.4-.6.7-1.1 1.8-.9 2.8 1 .1 2-.5 2.6-1.4z" />
    </svg>
  );
}

function WindowsIcon() {
  return (
    <svg width="26" height="26" viewBox="0 0 24 24" fill="currentColor" aria-hidden>
      <path d="M3 5.2 10.4 4.2v7.1H3zm8.2-1.2L21 2.5v8.8h-9.8zM3 12.7h7.4V20L3 18.9zm8.2.1H21V21.5l-9.8-1.4z" />
    </svg>
  );
}
