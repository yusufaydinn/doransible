import { Link } from "react-router-dom";

export function NotFoundPage() {
  return (
    <section>
      <h2>Sayfa bulunamadı</h2>
      <p>İstenen adres mevcut değil.</p>
      <Link to="/">Ana sayfaya dön</Link>
    </section>
  );
}
