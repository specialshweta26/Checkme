"use client";

import { useMemo, useState } from "react";
import "./grocery.css";

type Product = { id: number; name: string; detail: string; price: number; oldPrice?: number; category: string; emoji: string; color: string; badge?: string };

const products: Product[] = [
  { id: 1, name: "Avocados", detail: "Hass · 2 pieces", price: 89, oldPrice: 120, category: "Fresh produce", emoji: "🥑", color: "sage", badge: "25% off" },
  { id: 2, name: "Strawberries", detail: "Farm fresh · 250 g", price: 149, category: "Fresh produce", emoji: "🍓", color: "pink", badge: "Just picked" },
  { id: 3, name: "Sourdough loaf", detail: "Artisan bakery · 400 g", price: 125, category: "Bakery", emoji: "🍞", color: "sand" },
  { id: 4, name: "Free-range eggs", detail: "Brown · pack of 6", price: 78, category: "Dairy & eggs", emoji: "🥚", color: "cream" },
  { id: 5, name: "Baby spinach", detail: "Washed & ready · 100 g", price: 45, category: "Fresh produce", emoji: "🥬", color: "mint" },
  { id: 6, name: "Whole milk", detail: "Farm fresh · 1 litre", price: 68, category: "Dairy & eggs", emoji: "🥛", color: "blue" },
  { id: 7, name: "Heirloom tomatoes", detail: "Vine ripened · 500 g", price: 65, category: "Fresh produce", emoji: "🍅", color: "coral" },
  { id: 8, name: "Almond butter", detail: "No added sugar · 200 g", price: 249, category: "Pantry", emoji: "🥜", color: "sand", badge: "Bestseller" },
];
const categories = ["Everything", "Fresh produce", "Dairy & eggs", "Bakery", "Pantry"];
const money = (value: number) => `₹${value}`;

export default function GroceryPage() {
  const [category, setCategory] = useState("Everything");
  const [query, setQuery] = useState("");
  const [cart, setCart] = useState<Record<number, number>>({});
  const [cartOpen, setCartOpen] = useState(false);
  const [notice, setNotice] = useState("");
  const shown = useMemo(() => products.filter((p) => (category === "Everything" || p.category === category) && `${p.name} ${p.detail} ${p.category}`.toLowerCase().includes(query.toLowerCase())), [category, query]);
  const count = Object.values(cart).reduce((sum, n) => sum + n, 0);
  const subtotal = products.reduce((sum, p) => sum + p.price * (cart[p.id] || 0), 0);
  const change = (id: number, amount: number) => setCart((current) => ({ ...current, [id]: Math.max(0, (current[id] || 0) + amount) }));
  const checkout = () => { setNotice("Your basket is saved — checkout is coming soon!"); setCartOpen(false); window.setTimeout(() => setNotice(""), 3500); };

  return <main className="grocer">
    <div className="delivery-note">A little goodness, delivered in 30 minutes <span>✳</span> Free delivery over ₹499</div>
    <header className="g-header">
      <a className="g-brand" href="/grocery" aria-label="Goodroots home"><span className="brand-flower">✿</span><span>goodroots<small>THE NEIGHBOURHOOD MARKET</small></span></a>
      <label className="g-search"><span>⌕</span><input value={query} onChange={(e) => setQuery(e.target.value)} placeholder="Find your next favourite..." aria-label="Search groceries"/><kbd>⌘ K</kbd></label>
      <div className="g-actions"><button className="location"><span>⌖</span><span>Delivering to <b>Indiranagar, BLR</b></span><span>⌄</span></button><button className="basket-button" onClick={() => setCartOpen(true)} aria-label={`Open basket, ${count} items`}><span>♧</span> Basket <i>{count}</i></button></div>
    </header>
    <section className="g-hero"><div className="hero-copy"><span className="eyebrow"><span className="spark">✳</span> GOOD FOOD, GOOD MOOD</span><h1>A little closer<br/>to <em>the good stuff.</em></h1><p>Handpicked produce, pantry favourites, and the small-batch things that make everyday feel special.</p><a href="#shop" className="shop-link">Shop the good stuff <span>↘</span></a><div className="hero-proof"><span>★★★★★</span> <b>Loved by 2,400+ neighbours</b></div></div><div className="hero-art"><div className="hero-sun"></div><div className="hero-bag"><div className="bag-handle"></div><span>✿</span><b>GOOD<br/>THINGS</b></div><span className="floating fruit-one">🍊</span><span className="floating fruit-two">🍋</span><span className="floating leaf-one">✿</span><div className="hero-caption">A FRESH START, EVERY DAY <span>↗</span></div></div></section>
    <section className="promise-row"><div><span>✿</span><b>Picked at its peak</b><small>Fresh from farms we know</small></div><div><span>◷</span><b>At your door in 30</b><small>Good things don't wait</small></div><div><span>♡</span><b>Kind to the planet</b><small>Thoughtful, always</small></div><div><span>↗</span><b>Small shops, big love</b><small>Made by our neighbours</small></div></section>
    <section id="shop" className="shop-section"><div className="section-heading"><div><span className="eyebrow">THE GOOD STUFF</span><h2>Your everyday, <em>elevated.</em></h2></div><a className="view-all" href="#products">See what's fresh <span>→</span></a></div><div className="shop-tools"><div className="category-list" role="tablist" aria-label="Product categories">{categories.map((item) => <button key={item} role="tab" aria-selected={category === item} className={category === item ? "active" : ""} onClick={() => setCategory(item)}>{item}</button>)}</div><span className="result-count">{shown.length} GOOD FINDS</span></div>
      <div id="products" className="product-grid">{shown.map((product) => <article className="product-card" key={product.id}><div className={`product-art ${product.color}`}><span className="product-emoji">{product.emoji}</span>{product.badge && <span className="product-badge">{product.badge}</span>}<button className="heart" aria-label={`Save ${product.name}`} onClick={(e) => e.currentTarget.classList.toggle("saved")}>♡</button></div><div className="product-info"><span className="product-category">{product.category}</span><h3>{product.name}</h3><p>{product.detail}</p><div className="product-bottom"><div><b>{money(product.price)}</b>{product.oldPrice && <del>{money(product.oldPrice)}</del>}</div>{cart[product.id] ? <div className="quantity"><button onClick={() => change(product.id, -1)} aria-label={`Remove one ${product.name}`}>−</button><b>{cart[product.id]}</b><button onClick={() => change(product.id, 1)} aria-label={`Add one ${product.name}`}>+</button></div> : <button className="add-button" onClick={() => change(product.id, 1)} aria-label={`Add ${product.name} to basket`}>＋</button>}</div></div></article>)}</div>
      {shown.length === 0 && <div className="empty-search">No good finds just yet. Try another search or category.</div>}
    </section><footer className="g-footer"><a className="g-brand" href="/grocery"><span className="brand-flower">✿</span><span>goodroots<small>THE NEIGHBOURHOOD MARKET</small></span></a><span>Good food, grown with good people. <b>✳</b></span><span>© 2025 Goodroots Market</span></footer>
    {count > 0 && <button className="mobile-basket" onClick={() => setCartOpen(true)}>View basket · {count} items <b>{money(subtotal)}</b></button>}
    {cartOpen && <div className="drawer-backdrop" onClick={() => setCartOpen(false)}><aside className="cart-drawer" onClick={(e) => e.stopPropagation()}><div className="drawer-head"><div><span className="eyebrow">YOUR GOOD THINGS</span><h2>Your basket <em>({count})</em></h2></div><button className="close-drawer" onClick={() => setCartOpen(false)} aria-label="Close basket">×</button></div>{count === 0 ? <div className="cart-empty"><span>🧺</span><h3>Your basket is taking a nap.</h3><p>Add something lovely to get started.</p><button onClick={() => setCartOpen(false)}>Find something good</button></div> : <><div className="cart-lines">{products.filter((p) => cart[p.id]).map((p) => <div className="cart-line" key={p.id}><span className={`cart-emoji ${p.color}`}>{p.emoji}</span><div className="cart-name"><b>{p.name}</b><small>{p.detail}</small><div className="quantity"><button onClick={() => change(p.id, -1)}>−</button><b>{cart[p.id]}</b><button onClick={() => change(p.id, 1)}>+</button></div></div><b>{money(p.price * cart[p.id])}</b></div>)}</div><div className="cart-summary"><p><span>Subtotal</span><b>{money(subtotal)}</b></p><p><span>Delivery</span><b>{subtotal >= 499 ? "On us!" : money(35)}</b></p>{subtotal < 499 && <small>Add {money(499 - subtotal)} more for free delivery</small>}<button onClick={checkout}>Checkout · {money(subtotal + (subtotal >= 499 ? 0 : 35))} <span>→</span></button><small className="secure-note">Thoughtful groceries, securely yours.</small></div></>}</aside></div>}
    {notice && <div className="g-toast" role="status">✿ &nbsp;{notice}</div>}
  </main>;
}
