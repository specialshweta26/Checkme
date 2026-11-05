export const GITHUB_REPO = "specialshweta26/Checkme";
export const RELEASES_PAGE = `https://github.com/${GITHUB_REPO}/releases/latest`;

export type Platform = "mac" | "windows" | "other";
export type MacArch = "arm" | "intel";

export function detectPlatform(): Platform {
  if (typeof navigator === "undefined") return "other";
  const ua = navigator.userAgent || "";
  const plat = (navigator as Navigator & { userAgentData?: { platform?: string } }).userAgentData?.platform || navigator.platform || "";
  if (/Win/i.test(plat) || /Windows/i.test(ua)) return "windows";
  if (/Mac/i.test(plat) || /Mac OS|Macintosh/i.test(ua) || /iPhone|iPad|iPod/.test(ua)) return "mac";
  return "other";
}

export async function detectMacArch(): Promise<MacArch> {
  const nav = navigator as Navigator & {
    userAgentData?: { getHighEntropyValues?: (hints: string[]) => Promise<{ architecture?: string }> };
  };
  try {
    const data = await nav.userAgentData?.getHighEntropyValues?.(["architecture"]);
    if (data?.architecture === "x86") return "intel";
    if (data?.architecture === "arm") return "arm";
  } catch {
    /* ignore */
  }
  return "arm";
}

export type Installers = { macArm?: string; macIntel?: string; mac?: string; windows?: string; page: string };

export async function latestInstallers(): Promise<Installers> {
  try {
    const res = await fetch(`https://api.github.com/repos/${GITHUB_REPO}/releases/latest`, {
      headers: { Accept: "application/vnd.github+json" },
    });
    if (!res.ok) return { page: RELEASES_PAGE };
    const data = await res.json();
    const assets: { name?: string; browser_download_url?: string }[] = data.assets || [];
    const pick = (test: RegExp) => assets.find((a) => test.test(a.name || ""))?.browser_download_url;
    // Only installers: releases also hold update files like Lumio_aarch64.app.tar.gz.
    const macArm = pick(/(aarch64|arm64).*\.dmg$/i);
    const macIntel = pick(/(x64|x86_64).*\.dmg$/i);
    return {
      macArm,
      macIntel,
      mac: macArm || macIntel || pick(/\.dmg$/i),
      windows: pick(/\.(exe|msi)$/i),
      page: data.html_url || RELEASES_PAGE,
    };
  } catch {
    return { page: RELEASES_PAGE };
  }
}
