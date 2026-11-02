"use client";

import { useEffect, useState, type ReactNode } from "react";
import Link from "next/link";
import { SiteFooter, SiteNav } from "../components/SiteChrome";
import { detectMacArch, detectPlatform, latestInstallers, type Installers, type MacArch, type Platform } from "../downloads";
import { getMe } from "../lib";

const FIX = "xattr -cr /Applications/Lumio.app && open /Applications/Lumio.app";

export default function Download() {
  const [signedIn, setSignedIn] = useState(false);
  const [os, setOs] = useState<Platform>("other");
  const [macArch, setMacArch] = useState<MacArch>("arm");
  const [installers, setInstallers] = useState<Installers>({ page: "https://github.com/specialshweta26/Checkme/releases/latest" });
  const [origin, setOrigin] = useState("");
  const [copied, setCopied] = useState<"install" | "fix" | "">("");

  useEffect(() => {
    setOs(detectPlatform());
    setOrigin(window.location.origin);
    void detectMacArch().then(setMacArch);
    void latestInstallers().then(setInstallers);
    getMe().then((me) => setSignedIn(Boolean(me)));
  }, []);

  const installCmd = origin ? `curl -fsSL ${origin}/install-mac.sh | bash` : "curl -fsSL <site>/install-mac.sh | bash";
  const macHref = (macArch === "intel" ? installers.macIntel : installers.macArm) || installers.mac || installers.page;

  function copy(kind: "install" | "fix", text: string) {
    void navigator.clipboard.writeText(text).then(() => {
      setCopied(kind);
      window.setTimeout(() => setCopied(""), 2000);
    });
  }

  return (
    <div className="site">
      <SiteNav signedIn={signedIn} />
      <main className="site-download">
        <p className="site-kicker">Desktop</p>
        <h1>Lumio for Mac and Windows</h1>
        <p className="site-lead">
          The web app runs on a cloud computer. The desktop app uses this machine — files, apps, Chrome, and teaching — so
          bookings, logins, and skills stay on your computer.
        </p>
        {os === "mac" ? (
          <div className="site-install-box">
            <strong>Install on this Mac</strong>
            <p>
              Do not open a browser-downloaded disk image first. macOS will say Lumio is damaged and offer Move to Bin —
              that is Gatekeeper, not a broken file. Paste this in Terminal instead:
            </p>
            <pre className="site-install-cmd">{installCmd}</pre>
            <button type="button" className="btn" disabled={!origin} onClick={() => copy("install", installCmd)}>
              {copied === "install" ? "Copied" : "Copy command"}
            </button>
            <p className="site-download-note">
              Already copied Lumio to Applications? Run{" "}
              <button type="button" className="site-inline-cmd" onClick={() => copy("fix", FIX)}>
                {copied === "fix" ? "Copied" : FIX}
              </button>
            </p>
          </div>
        ) : null}
        <div className="site-os-grid">
          <OsCard
            title="macOS"
            hint={macArch === "intel" ? "Intel Mac, macOS 12+" : "Apple silicon, macOS 12+"}
            href={macHref}
            cta={installers.mac ? "Disk image (then run xattr)" : "Get the Mac build"}
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
        {os !== "mac" ? (
          <p className="site-download-note">
            On a Mac, do not open the downloaded disk image first — macOS will say Lumio is damaged. Install from Terminal
            with <code>{origin ? `curl -fsSL ${origin}/install-mac.sh | bash` : "curl -fsSL <site>/install-mac.sh | bash"}</code>, or after copying to Applications run{" "}
            <code>xattr -cr /Applications/Lumio.app</code>.
          </p>
        ) : null}
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
