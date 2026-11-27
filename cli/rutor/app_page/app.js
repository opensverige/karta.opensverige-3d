// The app page: eight steps from "can this machine draw" to "your cell waits for review".
//
// The page owns the Swedish. The app on this machine answers in short words ("mac", "taken")
// and the tables below turn each word into one sentence. Everything that comes from the app,
// the service, GitHub or a child process is put on the page as text, never as markup.

"use strict";

const OKANT = "Något gick fel. Försök igen.";

const ORD = {
  dator: {
    mac: "Det här är en Mac. Ritandet kräver ett NVIDIA-kort.",
    no_card: "Inget grafikkort som kan rita hittades. Det krävs ett NVIDIA-kort med 12 GB. Ett AMD-kort går också, men bara på Linux.",
    small_card: "Grafikkortet har för lite minne. Det krävs 12 GB.",
    old_driver: "Grafikkortets drivrutin är för gammal. Uppdatera den hos NVIDIA.",
    no_rocm: "AMD-kortet går inte att nå. Ditt konto behöver grupperna render och video.",
    disk: "Disken är för full. Gör plats och kom tillbaka.",
  },
  varning: {
    memory: "Datorn har lite minne. Ritandet kan gå långsamt eller avbrytas.",
  },
  nyckel: {
    missing: "Ingen nyckel är sparad än.",
    refused: "Cesium tog inte emot nyckeln. Skapa en ny och försök igen.",
    twice: "Nyckeln ser dubbel eller avklippt ut. Klistra in den en gång.",
    no_access: "Nyckeln får inte läsa kartbilden. Kontrollera kontot hos Cesium.",
    unreachable: "Cesium svarade inte. Försök igen om en stund.",
    broken: "Nyckeln har ett mellanslag i sig. Klistra in den igen.",
  },
  inlogg: {
    expired: "Koden godkändes inte i tid. Hämta en ny.",
    denied: "Inloggningen blev inte godkänd. Hämta en ny kod.",
    network: "Nätet svarade inte. Försök igen.",
    terms: "Säg ja till villkoren först.",
  },
  jobb: {
    taken: "Någon annan hann ta rutan. Välj en annan.",
    refused: "Tjänsten sa nej. Försök igen om en stund.",
    machine: "Något saknas på datorn. Titta på stegen igen.",
    network: "Nätet svarade inte. Försök igen.",
    check: "Bilden klarade inte kontrollen. Den skickades inte in.",
    failed: "Något gick fel. Försök igen.",
    locks: "Du har redan en låst ruta. Den släpps inom två dygn.",
    waiting: "Fem av dina rutor väntar på granskning. Vänta tills en är granskad.",
    lock_left: "En tidigare ruta är fortfarande låst. Försök igen om en stund.",
    old_lock: "Du har en låst ruta sedan förut. Tryck Försök igen, så släpps den och den här ritas.",
    daily: "Du har skickat in 20 rutor i dag. Fortsätt i morgon.",
    old_app: "Appen är för gammal för kartbladet. Hämta den nya versionen.",
    quota: "Cesium tar inte emot fler hämtningar från ditt konto just nu. Försök igen senare.",
    token: "Cesium tog inte emot nyckeln. Byt nyckel i steg 4.",
    flat: "Google har inga 3D-hus här, så rutan blir platt. Välj en annan plats.",
  },
  hamtning: {
    disk: "Disken blev full. Gör plats och tryck igen.",
    network: "Nätet svarade inte. Tryck igen så fortsätter hämtningen.",
    changed: "Det som hämtades stämde inte. Försök igen. Hjälper det inte behöver appen uppdateras.",
    failed: "Något gick fel med hämtningen. Kopiera felrapporten.",
  },
  svar: {
    busy: "Datorn är upptagen. Vänta tills det är klart.",
    not_an_app: "Den här installationen hämtar inget själv.",
    no_cell: "Välj en ruta först.",
    not_ready: "Något saknas i ett tidigare steg. Gå tillbaka och titta.",
    old_app: "Appen är för gammal för kartbladet. Hämta den nya versionen.",
    terms: "Säg ja till villkoren först.",
    none_free: "Inga rutor är lediga just nu. Titta in senare.",
    taken: "Rutan är inte ledig längre. Välj en annan.",
    bad_code: "Koden ser inte rätt ut. Kopiera den från kartan igen.",
  },
  blad: {
    none_free: "Inga rutor är lediga just nu. Titta in senare.",
    network: "Tjänsten svarade inte. Försök igen om en stund.",
  },
};

const VERKTYG = {
  torch: "PyTorch med CUDA",
  bitsandbytes: "bitsandbytes",
  render: "Pillow och NumPy",
  node: "Node 22 eller nyare",
  google_renderer: "Kartritaren i mappen cli/google-render",
};

const NAMN = ["Klarar datorn det?", "Villkoren", "Logga in", "Cesium-konto", "Ritverktyget", "Välj ruta", "Rita", "Klart"];
const FASER = ["claim", "render", "redraw", "upload"];
// The four things the app fetches in step 5, in the order it fetches them.
const HAMTAR = {
  stack: "Hämtar PyTorch",
  node: "Hämtar Node",
  renderer: "Hämtar kartverktyget",
  model: "Hämtar ritmodellen",
};

// The four places the page sends a person to. They are links the person presses. The page
// itself loads nothing from them.
const GITHUB_KOD = "https://github.com/login/device";
const CESIUM_ION = "https://ion.cesium.com";
const KARTANS_VARD = "karta.opensverige.se";
const NY_VERSION = "https://github.com/opensverige/karta.opensverige-3d/releases/";
const SAJTEN = "https://techembassy.se";
// The map of cells, where a person picks a cell anywhere in the country and copies its code.
const KARTAN = "https://karta.opensverige.se/pixelbygget";

const FRAGA_SEKUNDER = 2;
const FRAGA_SEKUNDER_UNDER_JOBB = 1;
const RUTNAT_SEKUNDER = 20;
const RUTNAT_BREDD = 620;
const TAPPAD_EFTER = 3;

let lage = null;
let visat = 0;
let forraFas = "";
let missar = 0;
let pagaende = null;
let nastaFraga = 0;

let rutnat = null;
let rutnatTid = 0;
let rutnatHamtas = false;
let rutnatMatt = null;
let vald = null;

let nyckelFel = "";
let bytNyckel = false;

// Closing the app: asked twice while something runs, and final once the app has said yes.
let stangFraga = false;
let stangd = false;
let stegFel = "";
let kodFel = "";
let vantaVisad = false;
let rapportVisad = false;
let upptagen = false;
// The step whose heading last got the focus, so that a step the page moves to by itself gets it too.
let fokuserat = 0;

// ---- small helpers

function el(id) {
  return document.getElementById(id);
}

function text(id, value) {
  const node = el(id);
  const wanted = String(value);
  if (node.textContent !== wanted) node.textContent = wanted;
}

function visa(id, on) {
  el(id).hidden = !on;
}

function ord(grupp, kod) {
  const tabell = ORD[grupp];
  if (tabell && typeof kod === "string" && Object.prototype.hasOwnProperty.call(tabell, kod)) return tabell[kod];
  return OKANT;
}

function gar(s) {
  return FASER.includes(s.job.phase);
}

function duger(s) {
  const delar = ["machine", "tools", "terms", "login", "cesium", "price", "sheet", "job", "install", "version"];
  return s !== null && typeof s === "object" && delar.every((del) => s[del] !== null && typeof s[del] === "object");
}

function rutnamn(cell) {
  const blad = lage.sheet;
  const namn = blad.id === cell.sheet && blad.name ? blad.name : cell.sheet;
  return namn + " " + cell.col + "_" + cell.row;
}

function klocka(iso) {
  const funnen = /^\d{4}-\d{2}-\d{2}T(\d{2}):(\d{2})/.exec(typeof iso === "string" ? iso : "");
  return funnen ? funnen[1] + "." + funnen[2] : "";
}

function kartlank(adress) {
  // The app passes on the map's own address or nothing. Checked once more before it is a link.
  try {
    const url = new URL(adress);
    return url.protocol === "https:" && url.hostname === KARTANS_VARD ? url.href : "";
  } catch (fel) {
    return "";
  }
}

async function skicka(vag, kropp) {
  try {
    const svar = await fetch(vag, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(kropp),
      cache: "no-store",
    });
    let data = null;
    try {
      data = await svar.json();
    } catch (fel) {
      data = null;
    }
    return { ok: svar.ok, data: data !== null && typeof data === "object" ? data : {} };
  } catch (fel) {
    return { ok: false, data: {} };
  }
}

// ---- where the person is

function klara(s) {
  const klart = s.job.phase === "done";
  return [
    s.machine.ok === true,
    s.terms.accepted === true,
    Boolean(s.login.login),
    s.cesium.ok === true,
    // In an app install the fetch has to be whole too: PyTorch answers long before the
    // model is all there.
    s.tools.ready === true && (s.install.app !== true || s.install.whole === true),
    Boolean(s.cell),
    klart,
    klart,
  ];
}

function framme(s) {
  if (s.job.phase === "done") return 8;
  if (gar(s) || s.job.phase === "failed" || s.job.phase === "stopped") return 7;
  const gjort = klara(s);
  for (let i = 0; i < 7; i += 1) {
    if (!gjort[i]) return i + 1;
  }
  return 7;
}

function ga(till) {
  const mal = Math.max(1, Math.min(Number(till) || 1, lage ? framme(lage) : 1));
  if (mal === visat) return;
  visat = mal;
  stegFel = "";
  kodFel = "";
  rita();
}

// ---- drawing the page from what the app says

function rita() {
  if (stangd) {
    // The app has ended. Nothing on the page can be pressed any more, and nothing is asked.
    for (const id of ["tappad", "gammal", "nyversion", "kropp", "fot"]) visa(id, false);
    el("mobilrad").parentElement.hidden = true;
    visa("stangd", true);
    return;
  }
  visa("tappad", missar >= TAPPAD_EFTER);
  if (!lage) return;
  const s = lage;
  const arbetar = gar(s) || Object.prototype.hasOwnProperty.call(HAMTAR, s.install.phase);
  if (!arbetar) stangFraga = false;
  for (const knapp of document.querySelectorAll("[data-stang]")) {
    knapp.textContent = stangFraga ? "Stäng ändå. Arbetet avbryts." : "Stäng appen";
    knapp.disabled = upptagen;
  }
  const fas = s.job.phase;
  const langst = framme(s);

  if (visat === 0) visat = langst;
  if (gar(s)) visat = 7;
  // A cell that has gone in has no question left to ask: step 7 would offer Rita again.
  else if (fas === "done" && visat === 7) visat = 8;
  else if (fas !== forraFas && fas === "done") visat = 8;
  else if (fas !== forraFas && (fas === "failed" || fas === "stopped")) visat = 7;
  if (visat > langst) visat = langst;
  if (fas !== forraFas) {
    rapportVisad = false;
    vantaVisad = false;
  }
  forraFas = fas;

  visa("gammal", s.version.current === false);
  el("gammal-lank").setAttribute("href", NY_VERSION);

  // Only a release page under the app's own name is ever a link here, whatever GitHub said.
  const nyApp = s.version.app;
  const nyLank =
    nyApp !== null &&
    typeof nyApp === "object" &&
    nyApp.current === false &&
    typeof nyApp.url === "string" &&
    nyApp.url.startsWith(NY_VERSION) &&
    !nyApp.url.includes("..");
  visa("nyversion", nyLank);
  el("nyversion-lank").setAttribute("href", nyLank ? nyApp.url : "");

  const gjort = klara(s);
  for (const knapp of el("rad").querySelectorAll("button")) {
    const n = Number(knapp.dataset.steg);
    // A tick is for what lies behind the person, never for a step they have not come to.
    const klar = gjort[n - 1] && n < langst;
    knapp.classList.toggle("klar", klar);
    knapp.disabled = n > langst || (gar(s) && n !== 7);
    if (n === visat) knapp.setAttribute("aria-current", "step");
    else knapp.removeAttribute("aria-current");
    const prick = knapp.querySelector(".prick");
    const tecken = klar ? "✓" : String(n);
    if (prick.textContent !== tecken) prick.textContent = tecken;
  }
  text("mobilrad", "Steg " + visat + " av 8 · " + NAMN[visat - 1]);
  el("mobilmatare").style.width = (visat / 8) * 100 + "%";

  for (let n = 1; n <= 8; n += 1) visa("steg-" + n, n === visat);
  STEG[visat - 1](s);
  // A new step starts at its heading, whoever moved there: the person, or the page when a
  // drawing started or ended. The button that was pressed is hidden by then, and a keyboard
  // or a screen reader would otherwise be left nowhere. Not on the first drawing of the page.
  if (visat !== fokuserat) {
    if (fokuserat !== 0) el("rubrik-" + visat).focus();
    fokuserat = visat;
  }
}

function steg1(s) {
  const m = s.machine;
  visa("dator-tittar", false);
  visa("dator-rader", true);
  const gammal = m.problem === "old_driver";
  const kortFel =
    m.problem === "mac" || m.problem === "no_card" || m.problem === "small_card" || m.problem === "no_rocm" || gammal;
  const kort = el("dator-kort");
  // An old driver is named with its number, so the person can see it change after an update.
  const drivrutin = gammal && m.driver ? " · drivrutin " + m.driver : "";
  text(
    "dator-kort",
    m.card ? m.card + " · " + m.card_gb + " GB" + drivrutin + (kortFel ? "" : " ✓") : "Inget grafikkort",
  );
  kort.classList.toggle("nej", kortFel);
  kort.classList.toggle("ok", !kortFel);
  const disk = el("dator-disk");
  const diskFel = m.problem === "disk";
  text("dator-disk", m.free_gb + " GB · " + m.needed_gb + " behövs" + (diskFel ? "" : " ✓"));
  disk.classList.toggle("nej", diskFel);
  disk.classList.toggle("ok", !diskFel);
  visa("dator-minnesrad", typeof m.ram_gb === "number");
  if (typeof m.ram_gb === "number") text("dator-minne", m.ram_gb + " GB");

  const besked = el("dator-besked");
  besked.hidden = false;
  besked.classList.toggle("nejruta", !m.ok);
  if (m.ok) {
    text("rubrik-1", "Klarar din dator det?");
    text("dator-besked", "Din dator klarar att rita.");
  } else {
    text("rubrik-1", "Den här datorn kan inte rita");
    text("dator-besked", ord("dator", m.problem));
  }
  visa("dator-varning", m.ok && Boolean(m.warning));
  if (m.warning) text("dator-varning", ord("varning", m.warning));
  visa("dator-ingress", m.ok);
  visa("dator-trost", m.problem === "mac");
  visa("dator-vidare", m.ok);
}

function steg2(s) {
  const lista = el("villkor");
  const rader = Array.isArray(s.terms.lines) ? s.terms.lines : [];
  if (lista.children.length !== rader.length) {
    lista.replaceChildren();
    for (const rad of rader) {
      // Word for word, numbers included, as the terms are written.
      const punkt = document.createElement("li");
      punkt.textContent = String(rad);
      lista.append(punkt);
    }
  }
  const kryss = el("villkor-ja");
  if (s.terms.accepted) {
    kryss.checked = true;
    kryss.disabled = true;
  }
  el("villkor-vidare").disabled = !kryss.checked || upptagen;
  visa("villkor-fel", Boolean(stegFel));
  text("villkor-fel", stegFel);
}

function steg3(s) {
  const l = s.login;
  const inne = Boolean(l.login) && !l.waiting;
  visa("inlogg-ingress", !inne);
  visa("inlogg-kod", l.waiting && Boolean(l.code));
  text("inlogg-kod", l.code || "");
  visa("inlogg-vantar", l.waiting === true);
  text("inlogg-vantar", l.code ? "Väntar på att du godkänner koden" : "Hämtar en kod från GitHub");
  visa("inlogg-klar", inne);
  if (inne) text("inlogg-klar", "Du är inloggad som " + l.login + ".");
  const fel = stegFel || (!l.waiting && !inne && l.problem ? ord("inlogg", l.problem) : "");
  visa("inlogg-fel", Boolean(fel));
  text("inlogg-fel", fel);

  visa("inlogg-start", !l.waiting && !inne);
  el("inlogg-start").disabled = upptagen;
  const oppna = el("inlogg-oppna");
  // Only GitHub's own device page is ever a link here, whatever the app said.
  const lankbar = l.waiting && Boolean(l.code) && l.url === GITHUB_KOD;
  if (lankbar) oppna.href = GITHUB_KOD;
  else oppna.removeAttribute("href");
  oppna.hidden = !lankbar;
  visa("inlogg-kopiera", l.waiting && Boolean(l.code));
  visa("inlogg-vidare", inne);
}

function steg4(s) {
  const c = s.cesium;
  const behovs = s.sheet.kind === "google" || !s.sheet.id;
  const klar = behovs && c.ok === true && !bytNyckel;
  const fraga = behovs && !klar;
  el("nyckel-lank").href = CESIUM_ION;
  visa("nyckel-ingress", behovs);
  visa("nyckel-tre", fraga);
  visa("nyckel-falt", fraga);
  visa("nyckel-prova", fraga);
  el("nyckel-prova").disabled = upptagen;
  visa("nyckel-klar", klar);
  visa("nyckel-behovs-inte", !behovs);
  visa("nyckel-vidare", c.ok === true && !fraga);
  visa("nyckel-byt", klar);
  let fel = "";
  if (fraga) {
    if (stegFel) fel = stegFel;
    else if (nyckelFel) fel = ord("nyckel", nyckelFel);
    else if (c.set && !c.ok && !bytNyckel) fel = ord("nyckel", c.problem);
  }
  visa("nyckel-fel", Boolean(fel));
  text("nyckel-fel", fel);
}

function gb(antal) {
  // Swedish writes the decimal with a comma.
  return (Math.round(antal * 10) / 10).toFixed(1).replace(".", ",");
}

function hamtning(s) {
  // Step 5 in an app install: the app fetches the tools itself, and says how far it is.
  const t = s.tools;
  const i = s.install;
  const klart = t.ready === true && i.whole === true;
  const gar = Object.prototype.hasOwnProperty.call(HAMTAR, i.phase);
  const stannat = !klart && !gar && i.phase === "stopped";
  const fel = !klart && !gar && i.phase === "failed";
  // Fetched to the end, and a tool is still missing: the fetch cannot put that right alone.
  const trasigt = !klart && !gar && i.phase === "done";
  const allt = typeof i.total_gb === "number" && i.total_gb > 0 ? i.total_gb : 0;
  const hamtat = typeof i.done_gb === "number" ? Math.min(Math.max(i.done_gb, 0), allt) : 0;
  const hela = Math.max(1, Math.ceil(allt));

  text("rubrik-5", klart ? "Ritverktyget" : gar ? "Ritverktyget hämtas" : "Hämta ritverktyget");
  if (klart) text("verktyg-ingress", "Allt som behövs finns på datorn.");
  else if (gar) text("verktyg-ingress", "Det tar en stund. Du kan göra annat under tiden.");
  else text("verktyg-ingress", hela + " GB, en gång. Det tar en stund.");

  visa("hamta-gar", gar);
  visa("hamta-rad", gar);
  if (gar) {
    const andel = allt > 0 ? Math.round((hamtat / allt) * 100) : 0;
    el("hamta-matare").style.width = andel + "%";
    el("hamta-stapel").setAttribute("aria-valuenow", String(andel));
    text("hamta-fas", HAMTAR[i.phase]);
    text("hamta-siffror", gb(hamtat) + " av " + hela + " GB");
  }

  // What is already on the disk is said, so that nobody thinks a second press starts over.
  const borjat = !klart && !gar && !fel && !trasigt && hamtat >= 0.1;
  visa("hamta-besked", stannat || borjat);
  if (stannat) text("hamta-besked", "Hämtningen stannade. Tryck igen så fortsätter den.");
  else if (borjat) text("hamta-besked", gb(hamtat) + " av " + hela + " GB finns redan på datorn.");

  const felrad = stegFel || (fel ? ord("hamtning", i.problem) : trasigt ? "Verktygen hämtades men svarar inte. Kopiera felrapporten." : "");
  visa("hamta-fel", Boolean(felrad));
  text("hamta-fel", felrad);
  visa("verktyg-saknas", false);
  visa("verktyg-hjalp", false);
  visa("verktyg-modell", false);

  // Every stop can be tried again: a file that did not match is already taken away.
  const igen = !klart && !gar;
  visa("hamta", igen);
  el("hamta").disabled = upptagen;
  text("hamta", stannat || borjat ? "Fortsätt hämta →" : fel || trasigt ? "Försök igen →" : "Hämta →");
  visa("hamta-avbryt", gar);
  el("hamta-avbryt").disabled = upptagen;
  const rapport = fel || trasigt;
  visa("hamta-kopiera", rapport);
  visa("hamta-rapport-rad", rapport && rapportVisad);
  visa("hamta-rapport", rapport && rapportVisad && Boolean(el("hamta-rapport").value));
  visa("verktyg-vidare", klart);
}

function steg5(s) {
  if (s.install.app === true) {
    hamtning(s);
    return;
  }
  // An install made by hand: the page says what is missing and installs nothing.
  for (const id of ["hamta-gar", "hamta-rad", "hamta-besked", "hamta-fel", "hamta", "hamta-avbryt", "hamta-kopiera", "hamta-rapport-rad", "hamta-rapport"]) {
    visa(id, false);
  }
  const t = s.tools;
  const saknas = Array.isArray(t.missing) ? t.missing : [];
  text("verktyg-ingress", t.ready ? "Allt som behövs finns på datorn." : "Det här saknas på datorn.");
  const lista = el("verktyg-saknas");
  const nyckel = saknas.join(",");
  if (lista.dataset.visar !== nyckel) {
    lista.dataset.visar = nyckel;
    lista.replaceChildren();
    for (const namn of saknas) {
      const punkt = document.createElement("li");
      const kant = typeof namn === "string" && Object.prototype.hasOwnProperty.call(VERKTYG, namn);
      punkt.textContent = kant ? VERKTYG[namn] : String(namn);
      lista.append(punkt);
    }
  }
  visa("verktyg-saknas", saknas.length > 0);
  visa("verktyg-hjalp", !t.ready);
  visa("verktyg-modell", t.ready === true && t.model_cached === false);
  visa("verktyg-vidare", t.ready === true);
}

function steg6(s) {
  const blad = s.sheet;
  const finns = Boolean(blad.id) && !blad.problem;
  // A cell by its code can be chosen whatever the sheet above says, also when nothing is free.
  visa("kod-del", true);
  visa("kod-fel", Boolean(kodFel));
  text("kod-fel", kodFel);
  el("kod-anvand").disabled = upptagen;
  const synligt = finns && rutnat !== null && rutnat.blad === blad.id && fonster(s) !== null;
  visa("ruta-ingress", finns);
  visa("ruta-karta", synligt);
  visa("ruta-forklaring", synligt);
  const fel = stegFel || (blad.problem ? ord("blad", blad.problem) : "");
  visa("ruta-fel", Boolean(fel));
  text("ruta-fel", fel);

  const mal = malruta(s);
  visa("ruta-vald", finns && mal !== null);
  if (mal !== null) text("ruta-namn", rutnamn(mal));
  visa("ruta-ta", finns && mal !== null);
  visa("ruta-nasta", finns);
  el("ruta-ta").disabled = upptagen;
  el("ruta-nasta").disabled = upptagen;

  if (finns) {
    const gammalt = Date.now() - rutnatTid > RUTNAT_SEKUNDER * 1000;
    if ((rutnat === null || gammalt || rutnat.blad !== blad.id) && !rutnatHamtas) hamtaRutnat();
    else ritaRutnat();
  }
}

function steg7(s) {
  const j = s.job;
  const pagar = gar(s);
  const slut = j.phase === "failed" || j.phase === "stopped";
  const fraga = !pagar && !slut;
  visa("rita-fraga", fraga);
  visa("rita-gar", pagar);
  visa("rita-slut", slut);

  const dyrt = s.price.decision === "WAIT";
  visa("rita-start", fraga);
  el("rita-start").disabled = upptagen || !s.cell;
  visa("rita-vanta", fraga && dyrt);
  visa("rita-rad", fraga && dyrt);
  visa("rita-avbryt", pagar);
  el("rita-avbryt").disabled = upptagen;
  // No "try again" where trying again gets the same answer: the cell is someone else's, the
  // day's limit is reached, or the app is too old for the sheet.
  visa("slut-igen", slut && !["taken", "daily", "old_app", "flat"].includes(j.problem));
  el("slut-igen").disabled = upptagen;
  // Nor another cell when the day's limit is reached: it would be drawn and then refused.
  // Nor when a lock from before stands in the way: it would stand in the way of that cell too.
  visa("slut-annan", slut && !["daily", "old_lock"].includes(j.problem));
  visa("slut-kopiera", j.phase === "failed");

  if (fraga) {
    text("rubrik-7", "Får datorn rita en stund?");
    text("rita-ruta", s.cell ? rutnamn(s.cell) : "");
    const pris = el("rita-pris");
    text("rita-pris", dyrt ? "högt just nu" : s.price.decision === "RUN" ? "lågt" : "okänt");
    pris.classList.toggle("ok", s.price.decision === "RUN");
    pris.classList.toggle("nej", dyrt);
    visa("rita-vanta-rad", vantaVisad && dyrt);
    if (vantaVisad) {
      const tid = klocka(s.price.after);
      text("rita-vanta-rad", tid ? "Priset väntas vara lågt efter kl. " + tid + ". Kom tillbaka då." : "Kom tillbaka i natt. Då är elen oftast billigare.");
    }
    visa("rita-fel", Boolean(stegFel));
    text("rita-fel", stegFel);
    return;
  }

  if (pagar) {
    text("rubrik-7", "Datorn ritar din ruta");
    const nu = FASER.indexOf(j.phase);
    for (const rad of el("faser").children) {
      const plats = FASER.indexOf(rad.dataset.fas);
      rad.classList.toggle("gjord", plats < nu);
      rad.classList.toggle("nu", plats === nu);
    }
    text("fas-rita-tid", s.tools.model_cached === false ? "12 GB hämtas först" : "ungefär 2 min");
    // A stop that came during the upload waits for it: the cell may already be with the service.
    text("rita-detalj", j.finishing ? "Skickar in klart först. Det tar några sekunder." : j.detail || "");
    return;
  }

  const avbrutet = j.phase === "stopped";
  text("rubrik-7", avbrutet ? "Ritandet avbröts" : "Rutan blev inte ritad");
  el("slut-besked").classList.toggle("nejruta", !avbrutet);
  // A lock from before is named, so that the person knows which cell is given back.
  const gammalt = j.problem === "old_lock" && j.old ? "Låst sedan förut: " + j.old + ". " : "";
  text("slut-besked", stegFel || (avbrutet ? "Ritandet stannade. Kortet är ledigt igen." : gammalt + ord("jobb", j.problem)));
  // Where the sentence above already says which cell is locked, or that none was, no more is said.
  const sagt = ["taken", "locks", "waiting", "lock_left", "old_lock"].includes(j.problem);
  visa("slut-las", !sagt);
  text("slut-las", j.held ? "Rutan är fortfarande låst. Låset släpps av sig självt inom två dygn." : "Rutan är inte låst. Inget skickades in.");
  visa("slut-rapport-rad", rapportVisad);
  visa("slut-rapport", rapportVisad && Boolean(el("slut-rapport").value));
}

function steg8(s) {
  const j = s.job;
  text("klart-ruta", rutnamn({ sheet: j.sheet, col: j.col, row: j.row }) + " · väntar på granskning");
  const lank = el("klart-karta");
  const adress = kartlank(j.url);
  if (adress) lank.href = adress;
  else lank.removeAttribute("href");
  lank.hidden = !adress;
}

const STEG = [steg1, steg2, steg3, steg4, steg5, steg6, steg7, steg8];

// ---- step 6: the cells of the sheet

function malruta(s) {
  if (vald !== null) return { sheet: s.sheet.id, col: vald.col, row: vald.row };
  // A cell that has been through a job is not offered again: it waits for review, or failed.
  if (s.cell && s.cell.sheet === s.sheet.id && s.job.phase === "idle") return s.cell;
  return null;
}

async function hamtaRutnat() {
  rutnatHamtas = true;
  try {
    const svar = await fetch("api/grid", { cache: "no-store" });
    const data = svar.ok ? await svar.json() : null;
    if (data && data.sheet && Array.isArray(data.cells)) {
      const rutor = new Map();
      for (const rad of data.cells) {
        if (Array.isArray(rad) && Number.isInteger(rad[0]) && Number.isInteger(rad[1]) && typeof rad[2] === "string") {
          rutor.set(rad[0] + "_" + rad[1], rad[2]);
        }
      }
      // On a sheet of the tiling only the cells someone has taken are listed. Any other is free.
      rutnat = {
        blad: String(data.sheet.id),
        cols: Number(data.sheet.cols) || 0,
        rows: Number(data.sheet.rows) || 0,
        fritt: data.sheet.made === "grid",
        rutor: rutor,
      };
      if (vald !== null && !ledig(vald.col, vald.row)) vald = null;
    }
  } catch (fel) {
    // The state says so when the service cannot be reached. The old grid stays meanwhile.
  }
  rutnatTid = Date.now();
  rutnatHamtas = false;
  if (lage && visat === 6) rita();
}

function ledig(col, row) {
  // Free: a listed cell that says so, or on a sheet of the tiling a cell that is not listed.
  if (rutnat === null) return false;
  const tillstand = rutnat.rutor.get(col + "_" + row);
  if (tillstand === undefined) return rutnat.fritt && col >= 0 && row >= 0 && col < rutnat.cols && row < rutnat.rows;
  return tillstand === "fri";
}

function fonster(s) {
  // The part of the sheet that is drawn: the cells that are offered and one cell around
  // them, so each gets room. On a sheet of the tiling the chosen cell counts too: it may be
  // the first cell anyone takes there. Null when there is nothing to draw.
  if (rutnat === null) return null;
  let minC = Infinity;
  let maxC = -Infinity;
  let minR = Infinity;
  let maxR = -Infinity;
  const med = (c, r) => {
    minC = Math.min(minC, c);
    maxC = Math.max(maxC, c);
    minR = Math.min(minR, r);
    maxR = Math.max(maxR, r);
  };
  for (const [plats, tillstand] of rutnat.rutor) {
    if (tillstand === "ute") continue;
    const delar = plats.split("_");
    med(Number(delar[0]), Number(delar[1]));
  }
  const mal = rutnat.fritt ? malruta(s) : null;
  if (mal !== null && mal.sheet === rutnat.blad) med(mal.col, mal.row);
  if (minC === Infinity) return null;
  return {
    minC: Math.max(0, minC - 1),
    minR: Math.max(0, minR - 1),
    maxC: Math.min(rutnat.cols - 1, maxC + 1),
    maxR: Math.min(rutnat.rows - 1, maxR + 1),
  };
}

function ritaRutnat() {
  if (rutnat === null || !lage) return;
  const ram = fonster(lage);
  if (ram === null) return;
  const minC = ram.minC;
  const minR = ram.minR;
  const kol = ram.maxC - minC + 1;
  const rad = ram.maxR - minR + 1;
  const sida = Math.max(3, Math.min(28, Math.floor(RUTNAT_BREDD / kol)));
  const skala = window.devicePixelRatio || 1;

  const duk = el("rutnat");
  const bredd = Math.round(kol * sida * skala);
  const hojd = Math.round(rad * sida * skala);
  if (duk.width !== bredd) duk.width = bredd;
  if (duk.height !== hojd) duk.height = hojd;
  // The frame is as wide as the cells drawn in it, plus its own padding on both sides.
  el("ruta-karta").style.maxWidth = kol * sida + 20 + "px";
  rutnatMatt = { minC: minC, minR: minR, kol: kol, rad: rad };

  const farg = getComputedStyle(document.documentElement);
  const farger = {
    live: farg.getPropertyValue("--moss-soft"),
    godkand: farg.getPropertyValue("--ochre"),
    vantar: farg.getPropertyValue("--ochre"),
    last: farg.getPropertyValue("--rust"),
    fri: farg.getPropertyValue("--yta"),
  };
  const penna = duk.getContext("2d");
  penna.setTransform(skala, 0, 0, skala, 0, 0);
  penna.clearRect(0, 0, kol * sida, rad * sida);
  const glipa = sida >= 6 ? 1 : 0;
  const rutor = new Map(rutnat.rutor);
  if (rutnat.fritt) {
    // The cells nobody has taken have no row. They are free, and drawn as free.
    for (let r = ram.minR; r <= ram.maxR; r += 1) {
      for (let c = minC; c <= ram.maxC; c += 1) {
        if (!rutor.has(c + "_" + r)) rutor.set(c + "_" + r, "fri");
      }
    }
  }
  for (const [plats, tillstand] of rutor) {
    if (!Object.prototype.hasOwnProperty.call(farger, tillstand)) continue;
    const delar = plats.split("_");
    const x = (Number(delar[0]) - minC) * sida;
    const y = (Number(delar[1]) - minR) * sida;
    penna.fillStyle = farger[tillstand];
    penna.fillRect(x, y, sida - glipa, sida - glipa);
    if (tillstand === "fri" && sida >= 6) {
      penna.strokeStyle = farg.getPropertyValue("--line-strong");
      penna.lineWidth = 1;
      penna.strokeRect(x + 0.5, y + 0.5, sida - glipa - 1, sida - glipa - 1);
    }
  }
  const mal = malruta(lage);
  if (mal !== null) {
    const x = (mal.col - minC) * sida;
    const y = (mal.row - minR) * sida;
    penna.fillStyle = farg.getPropertyValue("--yta");
    penna.fillRect(x, y, sida - glipa, sida - glipa);
    penna.strokeStyle = farg.getPropertyValue("--ink");
    penna.lineWidth = Math.max(2, sida / 8);
    penna.strokeRect(x - 1, y - 1, sida - glipa + 2, sida - glipa + 2);
    if (sida < 12) {
      // On a large sheet a cell is a few pixels wide. A ring shows where the chosen one is.
      penna.lineWidth = 1.5;
      penna.beginPath();
      penna.arc(x + sida / 2, y + sida / 2, 10, 0, 2 * Math.PI);
      penna.stroke();
    }
  }
}

function klickPaRutnat(handelse) {
  if (rutnat === null || rutnatMatt === null || upptagen) return;
  const duk = el("rutnat");
  const kant = duk.getBoundingClientRect();
  if (kant.width === 0 || kant.height === 0) return;
  const col = rutnatMatt.minC + Math.floor(((handelse.clientX - kant.left) / kant.width) * rutnatMatt.kol);
  const row = rutnatMatt.minR + Math.floor(((handelse.clientY - kant.top) / kant.height) * rutnatMatt.rad);
  if (ledig(col, row)) {
    vald = { col: col, row: row };
    stegFel = "";
  } else {
    stegFel = "Den rutan är inte ledig.";
  }
  rita();
}

// ---- asking the app, and telling it what the person pressed

function fraga() {
  // One question at a time. Whoever asks while one is under way gets that one's answer.
  if (pagaende === null) {
    pagaende = hamtaLage().finally(() => {
      pagaende = null;
    });
  }
  return pagaende;
}

async function farskFraga() {
  // After a press the page must see what the press changed. An answer that was already on
  // its way may be older than the press, so it is waited out and the app is asked anew.
  if (pagaende !== null) await pagaende;
  await fraga();
}

async function stang() {
  if (!lage || upptagen || stangd) return;
  const arbetar = gar(lage) || Object.prototype.hasOwnProperty.call(HAMTAR, lage.install.phase);
  if (arbetar && !stangFraga) {
    // A press by mistake must not end a drawing. The second press does.
    stangFraga = true;
    rita();
    return;
  }
  upptagen = true;
  rita();
  const svar = await skicka("api/quit", {});
  upptagen = false;
  if (svar.ok) {
    stangd = true;
    window.clearTimeout(nastaFraga);
    rita();
    el("rubrik-stangd").focus();
    return;
  }
  stangFraga = false;
  await farskFraga();
}

async function hamtaLage() {
  if (stangd) return;
  window.clearTimeout(nastaFraga);
  try {
    const svar = await fetch("api/state", { cache: "no-store" });
    const data = svar.ok ? await svar.json() : null;
    if (!duger(data)) throw new Error("state");
    lage = data;
    missar = 0;
  } catch (fel) {
    missar += 1;
  }
  rita();
  if (stangd) return;
  const paus = lage && gar(lage) ? FRAGA_SEKUNDER_UNDER_JOBB : FRAGA_SEKUNDER;
  nastaFraga = window.setTimeout(fraga, paus * 1000);
}

async function gor(vag, kropp, grupp) {
  // One press at a time: the buttons are shut until the app has answered.
  if (upptagen) return null;
  upptagen = true;
  stegFel = "";
  rita();
  const svar = await skicka(vag, kropp);
  upptagen = false;
  if (!svar.ok) stegFel = ord(grupp, svar.data.code);
  await farskFraga();
  return svar.ok ? svar.data : null;
}

async function kopiera(textAttKopiera) {
  try {
    await navigator.clipboard.writeText(textAttKopiera);
    return true;
  } catch (fel) {
    return false;
  }
}

function koppla() {
  document.addEventListener("click", (handelse) => {
    const mal = handelse.target instanceof Element ? handelse.target.closest("button") : null;
    if (!mal || !lage) return;
    if (mal.dataset.steg !== undefined) ga(mal.dataset.steg);
    else if (mal.dataset.till !== undefined) {
      if (mal.id === "klart-en-till" || mal.id === "slut-annan") {
        vald = null;
        rutnatTid = 0;
      }
      ga(mal.dataset.till);
    }
  });

  el("villkor-ja").addEventListener("change", rita);
  el("villkor-vidare").addEventListener("click", async () => {
    if (lage.terms.accepted || (await gor("api/terms", { accept: true }, "svar")) !== null) ga(3);
  });

  el("inlogg-start").addEventListener("click", () => gor("api/login", {}, "inlogg"));
  el("inlogg-kopiera").addEventListener("click", async () => {
    const gick = await kopiera(lage.login.code || "");
    text("inlogg-kopiera", gick ? "Kopierad" : "Markera koden och kopiera");
    window.setTimeout(() => text("inlogg-kopiera", "Kopiera koden"), 2500);
  });

  el("nyckel-prova").addEventListener("click", async () => {
    const falt = el("nyckel");
    const nyckel = falt.value.trim();
    if (!nyckel) {
      nyckelFel = "";
      stegFel = "Klistra in nyckeln först.";
      rita();
      return;
    }
    // The key is sent once and then leaves the page, whatever the answer is.
    falt.value = "";
    nyckelFel = "";
    const svar = await gor("api/cesium", { token: nyckel }, "svar");
    if (svar !== null && svar.ok === true) bytNyckel = false;
    else if (svar !== null) nyckelFel = typeof svar.problem === "string" ? svar.problem : "unreachable";
    rita();
  });
  el("nyckel").addEventListener("keydown", (handelse) => {
    if (handelse.key === "Enter") el("nyckel-prova").click();
  });
  el("nyckel-byt").addEventListener("click", () => {
    bytNyckel = true;
    nyckelFel = "";
    rita();
    el("nyckel").focus();
  });

  el("rutnat").addEventListener("click", klickPaRutnat);
  el("ruta-ta").addEventListener("click", async () => {
    const mal = malruta(lage);
    if (mal === null) return;
    if ((await gor("api/cell", { sheet: mal.sheet, col: mal.col, row: mal.row }, "svar")) !== null) {
      vald = null;
      ga(7);
    } else {
      rutnatTid = 0;
    }
  });
  el("ruta-nasta").addEventListener("click", async () => {
    kodFel = "";
    if ((await gor("api/cell", { next: true }, "svar")) !== null) {
      vald = null;
      rutnatTid = 0;
      ga(7);
    }
  });
  const anvandKod = async () => {
    // The code is sent as the person pasted it. The app takes it apart, and says only yes or no.
    kodFel = "";
    const svar = await gor("api/cell", { code: el("kod").value }, "svar");
    if (svar !== null) {
      el("kod").value = "";
      vald = null;
      rutnatTid = 0;
      ga(7);
    } else if (stegFel) {
      // The no belongs under the field it is about, not above the map.
      kodFel = stegFel;
      stegFel = "";
      rita();
    }
  };
  el("kod-anvand").addEventListener("click", anvandKod);
  el("kod").addEventListener("keydown", (handelse) => {
    if (handelse.key === "Enter") anvandKod();
  });

  el("rita-start").addEventListener("click", () => gor("api/build", {}, "svar"));
  el("slut-igen").addEventListener("click", () => gor("api/build", {}, "svar"));
  el("rita-avbryt").addEventListener("click", () => gor("api/stop", {}, "svar"));
  el("rita-vanta").addEventListener("click", () => {
    vantaVisad = true;
    rita();
  });
  // The same report from both places a thing can fail: the drawing and the fetch.
  for (const [knapp, ruta] of [["slut-kopiera", "slut-rapport"], ["hamta-kopiera", "hamta-rapport"]]) {
    el(knapp).addEventListener("click", async () => {
      let rapport = "";
      try {
        const svar = await fetch("api/report", { cache: "no-store" });
        rapport = svar.ok ? await svar.text() : "";
      } catch (fel) {
        rapport = "";
      }
      const gick = rapport !== "" && (await kopiera(rapport));
      // When the browser will not copy, the report is shown so that the person can.
      el(ruta).value = gick ? "" : rapport;
      rapportVisad = true;
      text(knapp, gick ? "Kopierad" : "Kopiera felrapport");
      window.setTimeout(() => text(knapp, "Kopiera felrapport"), 2500);
      rita();
    });
  }

  for (const knapp of document.querySelectorAll("[data-stang]")) knapp.addEventListener("click", stang);
  el("syfte-lank").setAttribute("href", SAJTEN);
  el("kod-karta").setAttribute("href", KARTAN);
  el("hamta").addEventListener("click", () => gor("api/install", {}, "svar"));
  el("hamta-avbryt").addEventListener("click", () => gor("api/install/stop", {}, "svar"));
}

koppla();
fraga();
