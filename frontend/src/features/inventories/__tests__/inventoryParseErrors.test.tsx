import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { linkedInventory } from "../../../test/fixtures";
import {
  errorResponse,
  installFetchMock,
  jsonResponse,
  renderApp,
  type RecordedRequest,
} from "../../../test/harness";

const DETAIL_ROUTE = `/inventories/${linkedInventory.id}`;

/** Detay isteği başarılı, içerik okuma verilen hatayla düşer. */
function installHostsFailure(response: unknown): void {
  installFetchMock((request: RecordedRequest) => {
    if (request.url.endsWith("/hosts")) {
      return response;
    }
    // Geçmiş ucu başarılı ve boş döner: bu dosya yalnızca **içerik** hatasını
    // ölçer ve ekranda tek bir hata kutusu bulunmasını bekler.
    if (request.url.includes("/ping-runs")) {
      return jsonResponse({ inventory_id: linkedInventory.id, items: [] });
    }
    return jsonResponse(linkedInventory);
  });
}

/** İçerik hatası ekranda görünene kadar bekler. */
async function findContentAlert(): Promise<HTMLElement> {
  await screen.findByRole("heading", { level: 2, name: linkedInventory.name });
  return screen.findByRole("alert");
}

describe("Inventory parse hataları", () => {
  it("parser kurulu değilse durumu açıklar ve kaydı gizlemez", async () => {
    installHostsFailure(
      errorResponse(
        503,
        "inventory_parser_unavailable",
        "Inventory parser çalıştırılamadı. `ansible-core` kurulu olmalıdır.",
      ),
    );

    renderApp(DETAIL_ROUTE);

    const alert = await findContentAlert();
    expect(alert).toHaveTextContent("Inventory parser kullanılamıyor");
    expect(alert).toHaveTextContent(/ansible-core kurulu değildir/i);
    // Parser çalışmasa da metadata görünmeye devam eder.
    expect(screen.getByText(linkedInventory.path)).toBeInTheDocument();
  });

  it("zaman aşımını açıklar ve tekrar denemeyi önerir", async () => {
    installHostsFailure(
      errorResponse(
        504,
        "inventory_parse_timeout",
        "Inventory parse işlemi zaman aşımına uğradı ve durduruldu.",
      ),
    );

    renderApp(DETAIL_ROUTE);

    const alert = await findContentAlert();
    expect(alert).toHaveTextContent("Inventory okuma zaman aşımına uğradı");
    expect(screen.getByRole("button", { name: "Tekrar dene" })).toBeInTheDocument();
  });

  it("stdout sınırı aşımını sonucun büyüklüğü olarak açıklar", async () => {
    installHostsFailure(
      errorResponse(
        502,
        "inventory_parse_output_too_large",
        "Inventory parser kabul edilen sınırdan çok çıktı üretti; işlem durduruldu.",
        { stream: "stdout" },
      ),
    );

    renderApp(DETAIL_ROUTE);

    const alert = await findContentAlert();
    expect(alert).toHaveTextContent("Inventory çıktısı boyut sınırını aştı");
    expect(alert).toHaveTextContent(/çözümlenmiş hâli kabul edilen boyut sınırını aştığı/i);
    expect(alert).not.toHaveTextContent(/hata metni ürettiği/i);
  });

  it("stderr sınırı aşımını hata metni taşması olarak açıklar", async () => {
    installHostsFailure(
      errorResponse(
        502,
        "inventory_parse_output_too_large",
        "Inventory parser kabul edilen sınırdan çok çıktı üretti; işlem durduruldu.",
        { stream: "stderr" },
      ),
    );

    renderApp(DETAIL_ROUTE);

    const alert = await findContentAlert();
    expect(alert).toHaveTextContent("Inventory çıktısı boyut sınırını aştı");
    expect(alert).toHaveTextContent(/hata metni ürettiği için işlem durduruldu/i);
    expect(alert).not.toHaveTextContent(/çözümlenmiş hâli/i);
  });

  it("tanınmayan stream değerinde genel mesaja düşer", async () => {
    installHostsFailure(
      errorResponse(
        502,
        "inventory_parse_output_too_large",
        "Sınır aşıldı.",
        { stream: "baska-bir-akis" },
      ),
    );

    renderApp(DETAIL_ROUTE);

    const alert = await findContentAlert();
    expect(alert).toHaveTextContent("Inventory çıktısı boyut sınırını aştı");
    expect(alert).toHaveTextContent(/kabul edilen boyut sınırından fazla çıktı ürettiği/i);
    // Doğrulanmamış değer ekrana hiç yazılmaz.
    expect(alert).not.toHaveTextContent("baska-bir-akis");
    expect(alert).not.toHaveTextContent("stream");
  });

  it("geçersiz parser çıktısını açıklar", async () => {
    installHostsFailure(
      errorResponse(
        502,
        "inventory_parse_invalid_output",
        "Parser çıktısı beklenen JSON sözleşmesine uymuyor.",
      ),
    );

    renderApp(DETAIL_ROUTE);

    const alert = await findContentAlert();
    expect(alert).toHaveTextContent("Parser çıktısı anlaşılamadı");
  });

  it("ayrıştırma hatasında temizlenmiş parser açıklamasını gösterir", async () => {
    const parserMessage =
      "Unable to parse <path> as an inventory source\n" +
      "  Syntax Error while loading YAML: mapping values are not allowed here";

    installHostsFailure(
      errorResponse(422, "inventory_parse_failed", "Inventory dosyası ayrıştırılamadı.", {
        parser_message: parserMessage,
      }),
    );

    renderApp(DETAIL_ROUTE);

    const alert = await findContentAlert();
    expect(alert).toHaveTextContent("Inventory dosyası ayrıştırılamadı");
    expect(alert).toHaveTextContent(/Syntax Error while loading YAML/);
    // Backend'in maskelediği yol yerine gerçek bir sunucu yolu uydurulmaz.
    expect(alert).toHaveTextContent("<path>");
    expect(alert).not.toHaveTextContent(linkedInventory.path);
    // Kök nedenin bu sonuçtan tek başına kesin sınıflandırılamayacağı söylenir;
    // "bu bir sunucu arızası değil" gibi kesin bir iddia yoktur (R1-V3J0B1).
    expect(alert).toHaveTextContent(/kök neden.*tek başına kesin biçimde sınıflandırılamaz/i);
    expect(alert).not.toHaveTextContent(/sunucu arızası değil/i);
  });

  it("parser_message string değilse hiç gösterilmez", async () => {
    installHostsFailure(
      errorResponse(422, "inventory_parse_failed", "Inventory dosyası ayrıştırılamadı.", {
        parser_message: { stderr: "Traceback (most recent call last)" },
      }),
    );

    renderApp(DETAIL_ROUTE);

    const alert = await findContentAlert();
    expect(alert).toHaveTextContent("Inventory dosyası ayrıştırılamadı");
    // Serileştirilmiş yapı kullanıcıya gösterilmez.
    expect(alert).not.toHaveTextContent("Traceback");
    expect(alert).not.toHaveTextContent("stderr");
    expect(alert.querySelector(".parser-message")).toBeNull();
  });

  it("boş parser_message için boş bir blok basmaz", async () => {
    installHostsFailure(
      errorResponse(422, "inventory_parse_failed", "Inventory dosyası ayrıştırılamadı.", {
        parser_message: "   ",
      }),
    );

    renderApp(DETAIL_ROUTE);

    const alert = await findContentAlert();
    expect(alert.querySelector(".parser-message")).toBeNull();
  });

  it.each([
    ["missing", "Inventory dosyası controller'da bulunamadı"],
    ["not_a_file", "Inventory yolu artık bir dosya değil"],
  ])("inventory_path_unavailable/%s durumunu açıklar", async (reason, expected) => {
    installHostsFailure(
      errorResponse(
        409,
        "inventory_path_unavailable",
        "Kayıtlı inventory dosyası kullanılabilir değil.",
        { inventory_id: linkedInventory.id, reason },
      ),
    );

    renderApp(DETAIL_ROUTE);

    const alert = await findContentAlert();
    expect(alert).toHaveTextContent(expected);
    expect(alert).not.toHaveTextContent("reason");
  });

  it("tanınmayan reason değerinde genel mesaja düşer", async () => {
    installHostsFailure(
      errorResponse(
        409,
        "inventory_path_unavailable",
        "Kayıtlı inventory dosyası kullanılabilir değil.",
        { inventory_id: linkedInventory.id, reason: "bilinmeyen_sebep" },
      ),
    );

    renderApp(DETAIL_ROUTE);

    const alert = await findContentAlert();
    expect(alert).toHaveTextContent("Inventory dosyası kullanılabilir değil");
    expect(alert).not.toHaveTextContent("bilinmeyen_sebep");
  });

  it("bilinmeyen details alanları ham JSON olarak gösterilmez", async () => {
    installHostsFailure(
      errorResponse(422, "inventory_parse_failed", "Inventory dosyası ayrıştırılamadı.", {
        parser_message: "Temizlenmiş açıklama.",
        internal_command: ["ansible-inventory", "--list", "-i", "/srv/gizli/hosts.ini"],
        debug_env: { ANSIBLE_CONFIG: "/tmp/xyz/ansible.cfg" },
        retry_count: 3,
      }),
    );

    renderApp(DETAIL_ROUTE);

    const alert = await findContentAlert();
    expect(alert).toHaveTextContent("Temizlenmiş açıklama.");

    // Bilinen alan dışındaki hiçbir şey ekrana çıkmaz.
    expect(alert).not.toHaveTextContent("internal_command");
    expect(alert).not.toHaveTextContent("ansible-inventory");
    expect(alert).not.toHaveTextContent("/srv/gizli/hosts.ini");
    expect(alert).not.toHaveTextContent("debug_env");
    expect(alert).not.toHaveTextContent("ANSIBLE_CONFIG");
    expect(alert).not.toHaveTextContent("retry_count");
    expect(alert).not.toHaveTextContent("{");
  });

  it("başarısız içerik okuması kendiliğinden tekrarlanmaz, kullanıcı tekrar deneyebilir", async () => {
    // Parser çağrısı sunucuda ayrı bir süreç başlatır; sessiz otomatik retry
    // kullanıcıya fayda sağlamadan maliyet üretirdi (hooks.ts).
    const { requests } = installFetchMock((request) => {
      if (request.url.endsWith("/hosts")) {
        return errorResponse(504, "inventory_parse_timeout", "Zaman aşımı.");
      }
      if (request.url.includes("/ping-runs")) {
        return jsonResponse({ inventory_id: linkedInventory.id, items: [] });
      }
      return jsonResponse(linkedInventory);
    });

    renderApp(DETAIL_ROUTE);
    await findContentAlert();

    const hostRequests = () =>
      requests.filter((request) => request.url.endsWith("/hosts")).length;
    expect(hostRequests()).toBe(1);

    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "Tekrar dene" }));

    await waitFor(() => expect(hostRequests()).toBe(2));
  });
});
