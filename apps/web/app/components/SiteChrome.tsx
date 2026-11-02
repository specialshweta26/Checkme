import Link from "next/link";
import { BrandMark } from "./BrandMark";

export function SiteNav({ signedIn = false }: { signedIn?: boolean }) {
  return (
    <header className="site-nav">
      <Link className="site-brand" href="/">
        <span className="site-mark">
          <BrandMark size={18} />
        </span>
        lumio
      </Link>
      <nav className="site-links" aria-label="Product">
        <Link href="/#product">Product</Link>
        <Link href="/download">Download</Link>
        {signedIn ? (
          <Link className="site-nav-cta" href="/chat">
            Open app
          </Link>
        ) : (
          <>
            <Link href="/login">Sign in</Link>
            <Link className="site-nav-cta" href="/chat">
              Open in browser
            </Link>
          </>
        )}
      </nav>
    </header>
  );
}

export function SiteFooter() {
  return (
    <footer className="site-footer">
      <div className="site-footer-row">
        <Link className="site-brand" href="/">
          <span className="site-mark">
            <BrandMark size={16} />
          </span>
          lumio
        </Link>
        <div className="site-footer-links">
          <Link href="/login">Sign in</Link>
          <Link href="/chat">Web app</Link>
          <Link href="/download">Mac</Link>
          <Link href="/download">Windows</Link>
        </div>
      </div>
      <p>Your private assistant for mail, calendar, browser, and the computer in front of you.</p>
    </footer>
  );
}
