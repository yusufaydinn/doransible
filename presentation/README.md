# DORAnsible sunum paketi

## Dosyalar

- `sunum.html`: İnternet gerektirmeyen, 16:9 ve tek dosya olarak taşınabilir HTML sunumu.
- `assets/architecture.svg`: UML bileşen diyagramının düzenlenebilir kaynak kopyası.
- `assets/ai-use-cases.svg`: Mevcut ve planlanan AI use-case diyagramının düzenlenebilir kaynak kopyası.

## Sunumu açma

Dosya yöneticisinden `sunum.html` dosyasını Firefox ile açın veya terminalde:

```bash
cd presentation
firefox sunum.html
```

Sunum tamamen yereldir; font, JavaScript veya görsel için internet bağlantısı
gerektirmez. Diyagramlar HTML içine gömülüdür. Bu nedenle yalnız
`sunum.html` dosyasını başka bir bilgisayara kopyalamak da yeterlidir;
`assets/` dizini sunumu görüntülemek için zorunlu değildir.

## Kontroller

| Tuş | İşlev |
|---|---|
| `→`, `Space`, `PageDown`, `Enter` | Sonraki slayt |
| `←`, `Backspace`, `PageUp` | Önceki slayt |
| `Home` / `End` | İlk / son slayt |
| `F` | Tam ekran |

## PDF alma

Firefox’ta `Ctrl+P` ile yazdırma ekranını açıp “Dosyaya yazdır / PDF” seçeneğini
kullanın. CSS her slaytı ayrı 16:9 sayfa olarak hazırlar.

## Sunum öncesi kontrol

1. Firefox’ta bütün slaytları ve iki SVG diyagramını açın.
2. Tam ekran görünümünde metin taşması olmadığını doğrulayın.
3. Demo ortamında worker, SSH erişimi ve hedef hostları kontrol edin.
4. Önceden tamamlanmış audit/remediation/idempotency Job kayıtlarını koruyun.
5. Canlı demo için kısa bir yerel ekran kaydını yedek olarak hazır tutun.
