// Runs in Qt's isolated ApplicationWorld on a Google Images results document.
// Read result metadata and control Google's own thumbnail buttons without running page-provided code.
(options => {
    "use strict";
    const normalize = s => s.trim().replace(/\s+/g, " ").replace(/[‘’]/g, "'").toLowerCase();
    const cardSelector = "[data-docid],[data-tbnid],[data-lpage],[data-iurl],[data-ou]";
    const searchHost = ["www.google.com","images.google.com","google.com"].includes(location.hostname);
    const accessOnly = options.action === "verified" || options.action === "access";
    // Qt can announce the destination while the previous or empty document is still current.
    if (options.loadingDocument && (!document.body || !searchHost ||
            normalize(new URL(location.href).searchParams.get("q") || "") !== normalize(options.query))) {
        return {state: "pending"};
    }
    if (!searchHost && !["sorry.google.com","consent.google.com"].includes(location.hostname)) {
        return accessOnly ? "waiting" : {state: "interaction"};
    }
    // A signed-out results page also has an account link in its header. Look for
    // the actual prompt beside a visible account control, including an overlay.
    const human = /\bsign in to (?:verify|confirm|prove) (?:that )?you(?:'re| are) (?:a human|human|a real person|not a (?:robot|bot))\b/;
    const login = 'a[href^="https://accounts.google.com/" i],form[action^="https://accounts.google.com/" i]';
    const dialogSelector = 'dialog[open],[aria-modal="true"],[role="dialog"],[role="alertdialog"]';
    const resultCard = cardSelector + ',a[href*="imgurl="]';
    const displayed = el => {
        const box = el.getBoundingClientRect();
        return box.width > 0 && box.height > 0 && !el.closest('[hidden],[inert],[aria-hidden="true"]') &&
            el.checkVisibility({visibilityProperty: true, opacityProperty: true});
    };
    const challengeControls = '#captcha-form, #captcha-form iframe, form[action*="/sorry/"], .g-recaptcha';
    const activeChallenge = Array.from(document.querySelectorAll(challengeControls)).some(displayed);
    if (activeChallenge) return accessOnly ? "waiting" : {state: "interaction"};
    const accountState = () => {
        const seen = new Set();
        const exposedResult = card => {
            if (!displayed(card)) return false;
            const rect = card.getBoundingClientRect();
            const left = Math.max(0, rect.left), right = Math.min(innerWidth, rect.right);
            const top = Math.max(0, rect.top), bottom = Math.min(innerHeight, rect.bottom);
            return right > left && bottom > top &&
                card.contains(document.elementFromPoint((left + right) / 2, (top + bottom) / 2));
        };
        for (const control of document.querySelectorAll(login)) {
            if (!displayed(control) || control.closest(resultCard)) continue;
            const dialog = control.closest(dialogSelector);
            if (dialog && Array.from(dialog.querySelectorAll('a,button,[role="button"]')).some(el =>
                displayed(el) && /^(?:not now|no thanks|continue without signing in)$/i.test(normalize(el.innerText || el.getAttribute('aria-label') || "")))) {
                return "interaction";  // Let the user make the choice in Google's own dialog.
            }
            let node = control.parentElement;
            for (let i = 0; node && i < 7 && !seen.has(node); ++i, node = node.parentElement) {
                seen.add(node);
                if (node.matches('nav,header,[role="navigation"],[role="banner"]') || node.querySelector(resultCard)) break;
                const copy = normalize(node.innerText || "");
                if (human.test(copy)) return "signin";
                const headings = node.querySelectorAll('h1,h2,[role="heading"]');
                if (Array.from(headings).some(h => displayed(h) && normalize(h.innerText || "") === "sign in to continue") &&
                        (dialog || !Array.from(document.querySelectorAll(resultCard)).some(exposedResult))) return "signin";
            }
        }
        if (!document.body || document.body.querySelector(resultCard)) return "";
        const copy = normalize((document.body.innerText || "").slice(0, 10000));
        return copy.includes("sign in to continue") && human.test(copy) ? "signin" : "";
    };
    const account = accountState();
    if (account) return accessOnly ? (account === "signin" ? "signin" : "waiting") : {state: account};
    if (options.action === "access") return "waiting";
    if (!searchHost || location.pathname.startsWith("/sorry")) {
        return accessOnly ? "waiting" : {state: "interaction"};
    }
    const urlQuery = new URL(location.href).searchParams.get("q") || "";
    if (!options.query || (!options.selectedDocument && !options.continuationDocument && !options.sourceDocument && normalize(urlQuery) !== normalize(options.query))) return {state: "different"};
    if (options.action === "verified") {
        // A 429 or a consent interstitial can keep the exact /search URL.
        const text = (document.body?.innerText || "").slice(0, 3500);
        if (document.readyState !== "complete" || !["/search", "/images", "/imgres"].includes(location.pathname)
                || Array.from(document.querySelectorAll('form[action*="consent.google"]')).some(displayed)
                || /our systems have detected unusual traffic|before you continue to google/i.test(text)) return "waiting";
        // A search shell can appear before Google has finished updating the page.
        const results = resultCard + ',:is(#search,#rso,#islrg) a[href] img';
        const hasVisibleResult = Array.from(document.querySelectorAll(results)).some(displayed);
        const empty = /\bno (?:matching images|(?:image )?results (?:found|for))\b|\bdid not match any (?:images|documents)\b/i.test(text);
        return hasVisibleResult || empty ? "results" : "waiting";
    }
    if (!document.body) return {state: "pending"};
    const candidateSelector = cardSelector + ',a[href*="imgurl="],a[href] img,img[data-original],img.sFlh5c,img[jsname="kn3ccd"]';
    const lazyAttributes = ["data-src","data-deferred-src","data-lazy-src","data-thumb","data-thumbnail-url","data-original"];
    const findUrl = value => {
        if (!value || typeof value !== "string" || value.length > 8192) return "";
        try {
            if (value.startsWith("//")) value = "https:" + value;
            const u = new URL(value);
            return u.protocol === "https:" && !u.username && !u.password && (!u.port || u.port === "443") ? u.href : "";
        } catch { return ""; }
    };
    const resolvePreview = raw => {
        if (!raw || typeof raw !== "string" || raw.length > 8192) return "";
        try { return findUrl(new URL(raw, document.baseURI).href); } catch { return ""; }
    };
    const inlineBytes = value => {
        if (typeof value !== "string" || value.length < 45 || value.length > 87420) return "";
        const bytes = /^data:image\/(?:jpeg|png|webp|gif);base64,([a-z0-9+/]{32,87384}={0,2})$/i.exec(value)?.[1];
        if (!bytes) return "";
        // A dimensionless pending <img> can already contain the correct bytes. Filter common
        // spacer headers here so an old 1x1 source never masks a newly assigned thumbnail.
        const header = atob(bytes.slice(0, 32));
        if (header.startsWith("GIF") && (header.charCodeAt(6) + 256 * header.charCodeAt(7) < 8 ||
                header.charCodeAt(8) + 256 * header.charCodeAt(9) < 8)) return "";
        if (header.startsWith("\x89PNG") && header.slice(12,16) === "IHDR") {
            const small = offset => header.slice(offset, offset + 3) === "\0\0\0" && header.charCodeAt(offset + 3) < 8;
            if (small(16) || small(20)) return "";
        }
        return bytes;
    };
    const googleThumb = url => {
        if (!url) return false;
        const u = new URL(url);
        return /(^|\.)(gstatic|ggpht)\.com$/i.test(u.hostname) ||
            (/(^|\.)google\.com$/i.test(u.hostname) && /(?:images|favicon|imgres|search)/i.test(u.pathname)) ||
            (/(^|\.)googleusercontent\.com$/i.test(u.hostname) && /\/proxy\//i.test(u.pathname));
    };
    const markOriginal = (raw, explicit) => {
        const url = findUrl(raw);
        if (!url || googleThumb(url)) return "";
        const u = new URL(url);
        if (/\.(?:svg|ico|html?|webm|mp4|zip|exe|js)(?:$|\/)/i.test(u.pathname)) return "";
        if (/\.(?:png|jpe?g|avif|bmp)(?:$|\/)/i.test(u.pathname)) return explicit && options.allowStills ? url : "";
        return explicit || /\.(?:gif|webp)(?:$|\/)/i.test(u.pathname) ||
            ["gif", "webp"].includes((u.searchParams.get("format") || "").toLowerCase()) ? url : "";
    };
    const metaUrl = (anchor, name) => {
        if (!anchor || !anchor.href) return "";
        try {
            const u = new URL(anchor.href);
            return ["www.google.com", "images.google.com", "google.com"].includes(u.hostname) ?
                findUrl(u.searchParams.get(name)) : "";
        } catch { return ""; }
    };
    const tidy = text => (text || "").replace(/\s+/g, " ").trim().slice(0, 220);
    const panelSelector = [
        "#islsp", "#Sva75c", ".islsp", "aside", '[role="dialog"]', '[role="complementary"]',
        '[aria-label*="image preview" i]', '[aria-label*="image details" i]',
        '[aria-label*="related images" i]', '[aria-label*="similar images" i]',
        '[data-attrid*="related" i]'
    ].join(",");
    const mainSelector = 'main,[role="main"],#search,#rso,#islrg';
    const controlSelector = 'a[href],button,[role="button"],[role="link"],input[type="button"]';
    const moreLabel = /^(?:(?:see|show|view|load) more(?: (?:related |similar )?(?:results|images))?|more (?:related |similar )?(?:results|images))$/i;
    const watching = options.action === "pixels";
    let state = globalThis.vhImages;
    if (watching && (!state || state.query !== options.query)) return {state:"pixels", items:[]};
    if (!state || state.query !== options.query) {
        if (state) {
            state.observer.disconnect();
            document.removeEventListener("load", state.onImageLoad, true);
        }
        state = {query: options.query, scripts: new WeakMap(), images: new Map(), deferred: new Map(), deferredBytes: 0, dirty: true,
            roots: new Map(), visited: new Set(), pager: new Set(),
            scope: options.inPanel && options.action !== "seed" ? "related" : "grid",
            activePanel: null, gridAncestors: new Set(), controlledPanel: null, sawPanel: false,
            selectedKey: "", selectedRow: null, panelBaseline: new Set(), oldPanelCards: new Map(),
            panelKeys: new Set(), panelPager: new Set(), armedButton: null, restoring: null, operation: -1,
            delivered: new Map(), posters: new Map(), committedPosters: new Map(),
            rendered: new Set(), committedRendered: new Set(), unreadable: new WeakMap()};
        state.onImageLoad = event => {
            const img = event.target;
            if (img instanceof HTMLImageElement &&
                    (img.closest(cardSelector + ",a[href]") || img.matches(candidateSelector))) state.dirty = true;
        };
        document.addEventListener("load", state.onImageLoad, true);
        state.observer = new MutationObserver(records => {
            state.dirty = true;
            for (const r of records) {
                const script = r.target.nodeName === "SCRIPT" ? r.target : r.target.parentElement;
                if (script?.nodeName === "SCRIPT") state.scripts.delete(script);
            }
        });
        state.observer.observe(document.documentElement, {subtree: true, childList: true,
            characterData: true, attributes: true, attributeFilter: ["src","srcset","sizes","data-srcset","data-tu","data-thumb-url","poster",
            ...lazyAttributes,"data-deferred","href","id","data-docid","data-ref-docid","data-tbnid","data-lpage","data-iurl","data-ou","alt","aria-label","aria-expanded","aria-hidden","aria-controls","hidden","style","class"]});
        globalThis.vhImages = state;
    }
    if (!watching && state.operation !== options.operation) {
        if (options.lastCompleted === state.operation) {
            for (const [key, stamp] of state.posters) state.committedPosters.set(key, stamp);
            for (const key of state.rendered) state.committedRendered.add(key);
        }
        state.operation = options.operation;
        state.delivered.clear();
        state.posters.clear();
        state.rendered.clear();
        state.restoring = null;
        state.dirty = true;
    }

    // Inline Google image records associate a thumbnail ID with the publisher's original.
    // Only JSON string tokens are decoded. Bound both script input and retained record count.
    const scanScripts = () => {
        let remaining = 5000000;
        for (const script of document.scripts) {
            if (script.src || state.scripts.has(script) || state.images.size >= 1500) continue;
            const text = script.textContent || "";
            if (text.length > remaining) continue;
            remaining -= text.length;
            state.scripts.set(script, true);
            // Google's deferred loader writes an exact thumbnail to named image IDs. Read only
            // its literal base64 bytes and the narrow ID list, never evaluate the script.
            if (text.length < 92000 && state.deferredBytes < 3 * 1024 * 1024 && text.includes("_setImagesSrc(")) {
                const match = /var s=['"](data:image\/(?:jpeg|png|webp|gif);base64,[A-Za-z0-9+/=\\xud]{32,90000})['"];\s*var ii=\[([^\]]{1,1200})\];\s*_setImagesSrc\(ii,s\)/i.exec(text);
                if (match) {
                    const inline = inlineBytes(match[1].replace(/\\(?:x3d|u003d)/gi, "="));
                    if (inline) {
                        for (const [, id] of match[2].matchAll(/['"](dimg_[\w-]{1,150})['"]/g)) state.deferred.set(id, inline);
                        state.deferredBytes += inline.length;
                    }
                }
            }
            if (!text.includes("https:")) continue;
            const record = /\[\s*(?:null|\d+)\s*,\s*"([\w:=-]{2,256})"\s*,\s*(?:\[\s*("[^"\r\n]{1,160000}")\s*,\s*\d+\s*,\s*\d+\s*(?:,[^\]\r\n]{0,160})?\]|null)\s*,\s*\[\s*("[^"\r\n]{1,8192}")\s*,\s*(\d+)\s*,\s*(\d+)\s*(?:,[^\]\r\n]{0,160})?\]/g;
            for (let match; (match = record.exec(text)) && state.images.size < 1500;) {
                try {
                    const original = markOriginal(JSON.parse(match[3]), true);
                    if (original) {
                        const thumbnail = match[2] && match[2].length <= 87420 ? JSON.parse(match[2]) : "";
                        const inline = state.deferredBytes < 3 * 1024 * 1024 ? inlineBytes(thumbnail) : "";
                        state.deferredBytes += inline.length;
                        state.images.set(match[1], {media: original, thumb: resolvePreview(thumbnail),
                            inline, width: +match[5], height: +match[4]});
                    }
                } catch { /* Another layout can still expose an original on the image or its link. */ }
            }
        }
    };
    const imageForRow = new WeakMap();
    // A result card can put its publisher's favicon before the actual artwork.
    const faviconUrl = value => typeof value === "string" && !value.startsWith("data:") &&
        /\/(?:favicons?(?:v\d+)?|apple-touch-icon|site[-_]?(?:icon|logo))(?:[./_?#-]|$)|\.ico(?:[?#]|$)/i.test(value);
    const smallSquare = img => img.naturalWidth > 0 && img.naturalWidth === img.naturalHeight && img.naturalWidth < 96;
    const siteIcon = img => faviconUrl(img.currentSrc) || faviconUrl(img.getAttribute("src")) ||
        faviconUrl(img.getAttribute("data-src")) ||
        /^(?:favicon|(?:site|website|publisher|source) (?:icon|logo))$/i.test(img.alt.trim()) ||
        (img.width > 0 && img.height > 0 && Math.max(img.width, img.height) <= 48 &&
            Math.max(img.naturalWidth, img.naturalHeight) > 2 * Math.max(img.width, img.height));
    const cardImage = element => {
        if (!element) return null;
        if (element.matches("img")) return siteIcon(element) ? null : element;
        const first = element.querySelector("img");
        if (!first) return null;
        // A 256px site logo is often drawn at 16px. Compare its displayed area
        // with the result artwork before consulting intrinsic image dimensions.
        const area = img => (img.width || img.naturalWidth) * (img.height || img.naturalHeight);
        let best = siteIcon(first) ? null : first;
        if (best && !smallSquare(best) && Math.min(best.width, best.height) > 48) return best;
        for (const img of element.querySelectorAll("img")) {
            if (siteIcon(img)) continue;
            if (!best || (smallSquare(best) !== smallSquare(img) ? !smallSquare(img) : area(img) > area(best))) best = img;
        }
        return best;
    };
    const inspect = node => {
        if (!(node instanceof Element)) return null;
        const card = node.closest(cardSelector);
        if (node.matches("img") && siteIcon(node)) return null;
        const img = cardImage(node) || cardImage(card);
        const anchor = node.closest("a[href]") || img?.closest("a[href]") ||
            card?.querySelector('a[href*="imgurl="]');
        let linkParams;
        if (findUrl(anchor?.href)) {
            const link = new URL(anchor.href);
            if (link.origin === location.origin && link.pathname === "/imgres") linkParams = link.searchParams;
        }
        const token = card?.dataset.tbnid || linkParams?.get("tbnid") || card?.dataset.docid || "";
        let meta = state.images.get(token);
        const src = findUrl(img?.currentSrc || img?.src);
        const originals = [metaUrl(anchor, "imgurl"), img?.dataset.iurl, img?.dataset.ou,
            card?.dataset.iurl, card?.dataset.ou, img?.dataset.original];
        let media = originals.map(raw => markOriginal(raw, true)).find(Boolean) || "";
        if (media && meta && media !== meta.media) meta = null;
        media ||= meta?.media || "";
        if (!media) media = markOriginal(src, Boolean(img?.matches('img.sFlh5c,img[jsname="kn3ccd"]'))) ||
            markOriginal(anchor?.href, false);
        let page = findUrl(card?.dataset.lpage) || metaUrl(anchor, "imgrefurl");
        if (!page && anchor?.href) {
            const href = findUrl(anchor.href);
            if (href) {
                const target = new URL(href);
                if (["www.google.com","images.google.com","google.com"].includes(target.hostname)) {
                    if (target.pathname === "/url") page = findUrl(target.searchParams.get("url") || target.searchParams.get("q"));
                } else {
                    page = href;
                }
            }
        }
        if (!media && !page) return null;
        const title = tidy(img?.alt || card?.querySelector("[role=heading],h3")?.textContent ||
            anchor?.title || card?.getAttribute("aria-label"));
        const distinctStill = (raw, namedThumbnail = false) => {
            const url = resolvePreview(raw);
            if (!url || url === media || url === page || faviconUrl(url)) return "";
            const u = new URL(url);
            if (["google.com","www.google.com","images.google.com"].includes(u.hostname)) {
                // A search or image viewer link is a document, even when it names a thumbnail.
                return u.pathname === "/images" && u.searchParams.has("q") ? url : "";
            }
            if (googleThumb(url) || namedThumbnail) return url;
            // A publisher's responsive still is often hosted separately from the original GIF.
            return /\.(?:jpe?g|png|webp)$/i.test(u.pathname) ||
                /^(?:jpg|jpeg|png|webp)$/i.test(u.searchParams.get("format") || u.searchParams.get("fm") || "")
                ? url : "";
        };
        const previewSources = [];
        if (img && !smallSquare(img)) previewSources.push(img.currentSrc, img.getAttribute("src"));
        previewSources.push(...lazyAttributes.map(name => img?.getAttribute(name)));
        previewSources.push(card?.dataset.tu, card?.dataset.thumbUrl, card?.dataset.thumbnailUrl, card?.dataset.thumb);
        const picture = img?.closest("picture");
        const sets = [img?.getAttribute("data-srcset"), img?.getAttribute("srcset")];
        for (const source of picture?.querySelectorAll("source") || []) {
            sets.push(source.getAttribute("data-srcset"), source.getAttribute("srcset"));
        }
        for (const srcset of sets) {
            if (srcset && srcset.length <= 180000) {
                for (const entry of srcset.split(/,\s*(?=(?:data:image\/|https?:|\/))/i, 6))
                    previewSources.push(entry.trim().split(/\s+/, 1)[0]);
            }
        }
        if (!img || smallSquare(img) || !img.currentSrc || img.naturalWidth <= 1) {
            // Limit background/poster reads to artwork identified inside this result.
            const art = card?.matches('[role="img"],[style*="background"]') ? card :
                card?.querySelector('[role="img"],[style*="background-image"]');
            if (art) {
                const bg = getComputedStyle(art).backgroundImage;
                const imageUrl = /^url\(\s*["']?(.*?)["']?\s*\)$/.exec(bg)?.[1];
                if (imageUrl) previewSources.push(imageUrl);
            }
            const poster = card?.querySelector("video[poster]")?.getAttribute("poster");
            if (poster) previewSources.push(poster);
        }
        let thumb = [meta?.thumb, metaUrl(anchor, "tbnurl"), card?.dataset.tu,
            card?.dataset.thumbUrl, card?.dataset.thumbnailUrl, card?.dataset.thumb]
            .map(value => distinctStill(value, true)).find(Boolean) || "";
        let inline = "";
        for (const value of previewSources) {
            if (!inline) inline = inlineBytes(value);
            if (!thumb) thumb = distinctStill(value);
            if (inline && thumb) break;
        }
        inline ||= state.deferred.get(img?.id) || meta?.inline || "";
        const doc = card?.dataset.refDocid || linkParams?.get("docid") ||
            (card?.dataset.tbnid ? card?.dataset.docid : "") || "";
        const row = {media, thumb, inline, page, title, id: token, doc,
            width: meta?.width || 0, height: meta?.height || 0};
        if (img) imageForRow.set(row, img);
        return row;
    };
    const visible = element => {
        if (!element || element.closest('[hidden],[inert],[aria-hidden="true"]')) return false;
        const box = element.getBoundingClientRect();
        return box.width > 4 && box.height > 4 && getComputedStyle(element).visibility !== "hidden";
    };
    const renderedPixels = (img, budget) => {
        if (!(img instanceof HTMLImageElement) || !img.complete || !img.currentSrc || smallSquare(img) ||
                img.naturalWidth < 8 || img.naturalHeight < 8 ||
                state.unreadable.get(img) === img.currentSrc) return null;
        // Chromium has already decoded this tile. Read a small first paint without
        // another request, including when its original data URL exceeds the inline limit.
        const scale = Math.min(1, 160 / Math.max(img.naturalWidth, img.naturalHeight));
        const width = Math.max(1, Math.round(img.naturalWidth * scale));
        const height = Math.max(1, Math.round(img.naturalHeight * scale));
        if (width < 8 || height < 8 || width * height * 4 > budget) return null;
        const canvas = document.createElement("canvas");
        canvas.width = width;
        canvas.height = height;
        try {
            const ctx = canvas.getContext("2d", {willReadFrequently: true});
            ctx.drawImage(img, 0, 0, width, height);
            const rgba = ctx.getImageData(0, 0, width, height).data;
            let bytes = "";
            for (let i = 0; i < rgba.length; i += 8192)
                bytes += String.fromCharCode(...rgba.subarray(i, i + 8192));
            return [width, height, btoa(bytes)];
        } catch {
            // Opaque cross-origin tiles use their ordinary Google thumbnail URL.
            state.unreadable.set(img, img.currentSrc);
            return null;
        }
    };
    if (watching) {
        // This read can overlap normal pagination in the same document. It must
        // never advance a delivery cursor, select a panel or consume a result.
        const body = document.body?.innerText.slice(0, 3500).toLowerCase() || "";
        if (body.includes("before you continue to google") || body.includes("unusual traffic")) return {state:"interaction"};
        const wanted = new Set(options.wanted);
        const items = [];
        let budget = Math.min(1048576, options.pixelBudget);
        const nodes = document.querySelectorAll(candidateSelector);
        for (let i = 0; i < Math.min(nodes.length, 4000) && wanted.size &&
                items.length < Math.min(24, options.pixelCount) && budget >= 256; i++) {
            const row = inspect(nodes[i]);
            if (!row) continue;
            const key = row.media || row.page;
            if (!wanted.has(key)) continue;
            const pixels = renderedPixels(imageForRow.get(row), budget);
            if (pixels) {
                wanted.delete(key);
                budget -= pixels[0] * pixels[1] * 4;
                items.push({key, pixels});
            }
        }
        return {state:"pixels", items};
    }
    if (options.action === "snapshot" && !state.dirty) return {state: "unchanged"};
    scanScripts();

    const labelOf = el => tidy(el.getAttribute("aria-label") || el.value || el.innerText || el.title);
    const enabled = el => visible(el) && !el.matches(':disabled,[aria-disabled="true"]');
    const relatedLink = url => {
        try {
            const u = new URL(url, location.href);
            if (u.origin !== location.origin || u.username || u.password ||
                    !["/search", "/images"].includes(u.pathname)) return null;
            return u;
        } catch { return null; }
    };
    const visualSearch = u => u && (u.pathname === "/images" || u.searchParams.get("udm") === "2" ||
        u.searchParams.get("tbm") === "isch" || /(?:^|,)rimg:/i.test(u.searchParams.get("tbs") || ""));
    const panelScopes = new Set();
    for (const el of document.querySelectorAll(panelSelector)) {
        if (!el.matches(mainSelector + "," + controlSelector + ",img") && visible(el)) panelScopes.add(el);
    }
    // Some Google viewer variants use ordinary divs for the right pane. Infer a
    // bounded viewer from its related-images heading, the selected hero image, or
    // an image-specific Google continuation. Never ascend into the main result grid.
    if (state.scope === "related" && !options.continuationDocument) {
        const seed = state.selectedRow || options;
        const intactSeed = state.selectedRow?.id || (options.seed || "").split(".")[0];
        const markers = document.querySelectorAll(
            'img.sFlh5c,img[jsname="kn3ccd"],[role="heading"],h2,h3,h4,a[href*="rimg"]'
        );
        for (const el of markers) {
            if (!visible(el) || el.closest(panelSelector)) continue;
            if (el.matches("img")) {
                if (!seed.media || ![el.src, el.currentSrc, el.dataset.iurl].includes(seed.media)) continue;
            } else if (el.matches("a")) {
                const u = relatedLink(el.href);
                if (!intactSeed || !visualSearch(u) || u.searchParams.get("tbnid") !== intactSeed) continue;
            } else if (el.closest(mainSelector) ||
                    !/^(related images|similar images|visually similar images|related results)$/i.test(tidy(el.innerText))) {
                continue;
            }
            let region = null;
            for (let parent = el.parentElement; parent && parent !== document.body;
                    parent = parent.parentElement) {
                if (parent.matches(mainSelector) || parent.querySelector(mainSelector) ||
                        state.gridAncestors.has(parent)) break;
                if (parent.querySelector(candidateSelector) && !parent.matches(cardSelector)) region = parent;
            }
            if (region) panelScopes.add(region);
        }
    }
    state.controlledPanel = state.controlledId ? document.getElementById(state.controlledId) : null;
    if (visible(state.controlledPanel) && !state.controlledPanel.matches(mainSelector) &&
            !state.gridAncestors.has(state.controlledPanel)) panelScopes.add(state.controlledPanel);
    // Treat nested components (e.g. 'related images' within a dialog) as one viewer.
    const scopes = [...panelScopes].filter(p => ![...panelScopes].some(q => p !== q && q.contains(p)));
    const rows = new Map();
    const targets = new Map();
    const gridCards = new Set();
    const entries = [];
    const main = document.querySelector('#islrg') || document.querySelector('#rso') ||
        document.querySelector('main,[role="main"],#search') || document.body;
    const continued = Boolean(options.continuationDocument && options.action !== "seed");
    const nodes = document.querySelectorAll(candidateSelector);
    for (let i = 0; i < Math.min(nodes.length, 4000); i++) {
        const node = nodes[i];
        const row = inspect(node);
        if (!row) continue;
        const key = row.media || row.page;
        const card = node.closest(cardSelector) || node;
        const panel = scopes.find(el => el.contains(node));
        if (continued && (panel || !main.contains(node) || node.closest('header,footer,nav,[role="navigation"]'))) continue;
        const prior = rows.get(key);
        if (!prior) rows.set(key, row);
        else {
            // Nested markup may expose the original address first and its preview on a later node.
            prior.thumb ||= row.thumb;
            prior.inline ||= row.inline;
            const candidate = imageForRow.get(row);
            const first = imageForRow.get(prior);
            if (candidate && (!first || first.naturalWidth < 8 || smallSquare(first)) &&
                    candidate.naturalWidth >= 8 && !smallSquare(candidate)) imageForRow.set(prior, candidate);
        }
        if (!panel) gridCards.add(card);
        const occurrence = {key, row, card, panel, target: null};
        entries.push(occurrence);
        const image = cardImage(card);
        if (!visible(image)) continue;
        let control = image.closest('button,[role="button"][jsaction]');
        const anchor = image.closest("a[href]");
        if (anchor) {
            if (!findUrl(anchor.href)) continue;
            const u = relatedLink(anchor.href);
            const imgres = new URL(anchor.href);
            if (!u && !(imgres.origin === location.origin && imgres.pathname === "/imgres")) {
                // A Google button enclosing a publisher anchor can be activated
                // directly. Clicking the anchor itself would leave image search.
                if (!control || anchor.contains(control) || control.matches("a[href]")) continue;
            }
        }
        if (!control || !(card.contains(control) || control.contains(card))) control = image;
        occurrence.target = {card, image, control};
        if (!targets.has(key) || state.activePanel?.contains(node)) targets.set(key, occurrence.target);
        if (state.scope === "grid" && !panel && !state.panelKeys.has(key) &&
                !state.roots.has(key) && state.roots.size < 1000) {
            state.roots.set(key, {media: row.media, page: row.page, id: row.id, doc: row.doc,
                location: card.getBoundingClientRect().top + scrollY});
        }
    }

    // Consent and unusual traffic must pause even if a document includes stale result markup.
    const bodyText = (document.body?.innerText || "").slice(0, 3500).toLowerCase();
    if (bodyText.includes("our systems have detected unusual traffic") ||
            bodyText.includes("before you continue to google") ||
            (!rows.size && bodyText.includes("unusual traffic"))) return {state: "interaction"};

    // One media URL can occur on several source pages with different Google IDs.
    // Choose a concrete image control before collapsing duplicates for the picker.
    const selectedOccurrence = () => {
        const candidates = entries.concat([...state.roots].map(([key, row]) => ({key, row})));
        const matches = candidates.filter(({row}) => {
            const identifier = row.id && row.doc ? row.id + "." + row.doc : "";
            const [requestedId, requestedDoc] = (options.seed || "").split(".");
            if ((requestedId && row.id && requestedId !== row.id) ||
                    (requestedDoc && row.doc && requestedDoc !== row.doc)) return false;
            if (options.page && row.page && row.page !== options.page) return false;
            if (options.media && row.media && row.media !== options.media) return false;
            if (options.seed && identifier === options.seed) return true;
            if (options.media && row.media === options.media) return true;
            return options.page && row.page === options.page;
        });
        if (!matches.length) return null;
        const score = ({row}) =>
            Number(Boolean(options.seed && row.id + "." + row.doc === options.seed)) * 4 +
            Number(Boolean(options.media && row.media === options.media)) * 2 +
            Number(Boolean(options.page && row.page === options.page));
        const best = Math.max(...matches.map(score));
        const tied = matches.filter(entry => score(entry) === best);
        if (["id", "doc", "media", "page"].some(field =>
                new Set(tied.map(e => e.row[field]).filter(Boolean)).size > 1)) return null;
        return tied.find(e => e.target && state.activePanel?.contains(e.card)) ||
            tied.find(e => e.target) || tied[0];
    };
    if (options.action === "seed") {
        const explicit = Boolean(options.media || options.page || options.seed);
        const chosen = explicit ? selectedOccurrence() : null;
        const key = explicit ? chosen?.key :
            ([...state.roots.keys()].find(k => !state.visited.has(k) && targets.has(k)) ??
            [...state.roots.keys()].find(k => !state.visited.has(k)));
        if (!key && options.branch) {
            state.restoring ??= {key: "", since: performance.now()};
            if (performance.now() - state.restoring.since < 1800) return {state: "restoring"};
        }
        if (!key || (!options.branch && state.visited.has(key))) return {state: "unavailable"};
        const target = explicit ? chosen.target : targets.get(key);
        if (!target) {
            const root = explicit ? chosen.row : state.roots.get(key);
            if (Number.isFinite(root?.location) && state.restoring?.key !== key) {
                state.restoring = {key, since: performance.now()};
                window.scrollTo({top: Math.max(0, root.location - innerHeight / 2), behavior: "instant"});
                return {state: "restoring"};
            }
            if (state.restoring?.key === key && performance.now() - state.restoring.since < 1600) return {state: "restoring"};
            // Do not follow a new card that recycled the old root's DOM node.
            state.visited.add(key);
            return {state: "unavailable"};
        }
        state.visited.add(key);
        const chosenRow = explicit ? chosen.row : (rows.get(key) || state.roots.get(key));
        const alreadyOpen = options.branch && state.selectedKey === key &&
            ["id", "doc", "page"].every(field => state.selectedRow?.[field] === chosenRow[field]) &&
            state.activePanel && scopes.includes(state.activePanel);
        state.scope = "related";
        state.selectedKey = key;
        state.selectedRow = chosenRow;
        state.sawPanel = false;
        state.dirty = true;
        if (alreadyOpen) return {state: "clicked", root: state.selectedRow};
        state.activePanel = null;
        state.armedButton = null;
        state.beforeContinuation = null;
        state.panelPager.clear();
        state.controlledPanel = null;
        state.controlledId = target.control.closest("[aria-controls]")?.getAttribute("aria-controls") || "";
        state.oldPanelCards = new Map(entries.filter(e => e.panel).map(e => [e.card, e.key]));
        state.panelBaseline = new Set(scopes);
        state.gridAncestors.clear();
        for (const card of gridCards) {
            for (let el = card; el && !state.gridAncestors.has(el); el = el.parentElement)
                state.gridAncestors.add(el);
        }
        target.image.scrollIntoView({block: "nearest", behavior: "instant"});
        target.control.click();
        state.dirty = true;
        return {state: "clicked", root: chosenRow};
    }

    const scrollBox = (start, fallback) => {
        for (let el = start; el && el !== document.body; el = el.parentElement) {
            if (el.scrollHeight > el.clientHeight + 8 && /(auto|scroll)/.test(getComputedStyle(el).overflowY)) return el;
        }
        return fallback;
    };
    // Bind collection to a single panel after the selected card is activated.
    // A recycled panel may briefly show the previous card's results; retain the
    // pre-click card identities so these stale nodes do not leak into the branch.
    if (state.scope === "related" && !continued && !scopes.includes(state.activePanel)) {
        state.activePanel = null;
        const fresh = scopes.filter(scope => !state.panelBaseline.has(scope) ||
            entries.some(e => e.panel === scope && state.oldPanelCards.get(e.card) !== e.key));
        if (state.controlledId) {
            // The selected thumbnail explicitly owns this viewer. Another panel may
            // mount earlier while the named viewer is still being populated.
            state.activePanel = fresh.find(p => state.controlledPanel && p.contains(state.controlledPanel)) || null;
        } else {
            const selectedKey = state.selectedKey || options.media;
            const matched = fresh.find(p => entries.some(e => e.panel === p && e.key === selectedKey));
            if (matched || fresh.length === 1) state.activePanel = matched || fresh[0];
        }
    }
    // A button can push a new URL before replacing the old results. While that
    // original grid is still present, treat the continuation as loading. A full
    // document navigation normally creates a fresh isolated world instead.
    const replacingGrid = continued && state.beforeContinuation &&
        entries.some(e => state.beforeContinuation.get(e.card) === e.key && visible(e.card));
    const panel = continued ? (replacingGrid ? null : main) : state.activePanel;
    const panelRows = new Map();
    if (panel) {
        for (const e of entries) {
            if (!continued && (e.panel !== panel || state.oldPanelCards.get(e.card) === e.key)) continue;
            if (e.key === state.selectedKey || (options.media && e.row.media === options.media)) continue;
            if (!panelRows.has(e.key)) panelRows.set(e.key, e);
        }
    }
    const panelMounted = Boolean(panel);
    const branchMember = key => panelRows.has(key);
    if (options.action === "panel" || options.action === "activateMore") {
        if (!panel) return {state: "panelWaiting"};
        const fresh = [...panelRows.values()];
        if (options.action === "panel" && fresh.some(e => !state.delivered.has(e.key)) &&
                state.inspectedOperation === options.operation) {
            state.dirty = true;
            return {state: "panelWaiting"};
        }
        const box = scrollBox(fresh[0]?.card || panel, continued ? document.scrollingElement : null);
        if (options.action === "panel" && box && (continued || (panel.contains(box) && !state.gridAncestors.has(box))) &&
                box.scrollHeight > box.scrollTop + box.clientHeight + 2) {
            box.scrollBy({top: Math.max(250, box.clientHeight * .9), behavior:"instant"});
            state.dirty = true;
            return {state:"scrolled"};
        }
        // Only explicit branches consume the selected panel's own continuation.
        // Skip visually identical publisher links and controls from another panel.
        const controls = options.branch ? panel.querySelectorAll(controlSelector) : [];
        let sawFooter = false;
        for (const footer of controls) {
            if (!enabled(footer) || footer.closest('nav[aria-label="Main"],header') ||
                    (continued && scopes.some(host => host.contains(footer)))) continue;
            const label = labelOf(footer);
            const anchor = footer.closest("a[href]");
            const u = anchor ? relatedLink(anchor.href) : null;
            const samePage = anchor && anchor.href.split("#")[0] === location.href.split("#")[0];
            if (anchor && !samePage && !visualSearch(u)) continue;
            const token = state.selectedRow?.id || (options.seed || "").split(".")[0];
            const forImage = u?.searchParams.get("tbnid");
            if (forImage && token && forImage !== token) continue;
            // Some viewers localize the text. Google's image-specific related link
            // carries the clicked thumbnail ID as well as an opaque related-search token.
            const tiedToSeed = token && forImage === token && /(?:^|,)rimg:/i.test(u?.searchParams.get("tbs") || "");
            if (!moreLabel.test(label) && !tiedToSeed && !(continued && /^(next|next page)$/i.test(label))) continue;
            sawFooter = true;
            const marker = u && !samePage ? u.href :
                [state.selectedKey, label, panelRows.size, fresh.at(-1)?.key].join("|");
            if (state.panelPager.has(marker)) continue;
            if (u && !samePage) {
                if (options.action === "activateMore") continue;
                state.panelPager.add(marker);
                state.beforeContinuation = new Map(entries.filter(e => !e.panel).map(e => [e.card, e.key]));
                return {state:"continuation", url: u.href};
            }
            if (options.action !== "activateMore") {
                state.armedButton = footer;
                return {state:"continuationButton"};
            }
            if (state.armedButton !== footer) continue;
            state.armedButton = null;
            state.panelPager.add(marker);
            state.beforeContinuation = new Map(entries.filter(e => !e.panel).map(e => [e.card, e.key]));
            footer.scrollIntoView({block:"nearest", behavior:"instant"});
            footer.click();
            state.dirty = true;
            return {state:"expanded"};
        }
        if (options.action === "activateMore") return {state: "panelWaiting"};
        state.dirty = true;
        return {state: fresh.length || sawFooter ? "panelEnd" : "panelWaiting"};
    }
    if (options.action === "page") {
        state.scope = "grid";
        const mainRoot = [...targets].find(([key, target]) => state.roots.has(key) && !target.card.closest(panelSelector));
        const box = scrollBox(mainRoot?.[1].card, document.scrollingElement);
        if (!box) return {state: "unavailable"};
        // The current document's originals have already been returned. Advance to the loading
        // sentinel at the end, then let the browser's actual infinite-scroll handler run.
        const controls = document.querySelectorAll('a#pnnext,a[aria-label],button,[role="button"],input[type="button"]');
        for (const control of controls) {
            const label = tidy(control.getAttribute("aria-label") || control.value || control.textContent).toLowerCase();
            if (!(control.id === "pnnext" || /^(show more results|more results|see more results|next page|next)$/.test(label)) ||
                    !visible(control) || control.closest(panelSelector) || control.matches(":disabled,[aria-disabled=true]")) continue;
            const anchor = control.closest("a[href]");
            let destination = "";
            if (anchor) {
                try {
                    const url = new URL(anchor.href);
                    if (url.origin !== location.origin || url.pathname !== "/search" ||
                            normalize(url.searchParams.get("q") || "") !== normalize(options.query)) continue;
                    destination = url.href;
                } catch { continue; }
            }
            const key = destination || [rows.size, box.scrollHeight, [...rows.keys()].pop(),label].join("|");
            if (state.pager.has(key)) continue;
            state.pager.add(key);
            control.scrollIntoView({block: "nearest", behavior: "instant"});
            control.click();
            state.dirty = true;
            return {state: "scrolled"};
        }
        const marker = [rows.size, box.scrollHeight, [...rows.keys()].pop()].join("|");
        if (state.pager.has(marker)) return {state: "unavailable"};
        state.pager.add(marker);
        box.scrollTo({top: box.scrollHeight, behavior: "instant"});
        state.dirty = true;
        return {state: "scrolled"};
    }

    // Stream large DOMs over successive bounded snapshots. A new operation starts its own
    // delivery cursor, so canceling mid-request cannot suppress results in the next request.
    const items = [];
    let more = false;
    if (state.scope === "related" && panelMounted && !state.sawPanel) {
        // A panel can mount after the grid has hydrated. Re-emit overlapping keys
        // from the panel itself so the native collector can discard provisional rows.
        state.delivered.clear();
        state.sawPanel = true;
    }
    let budget = Math.max(0, Math.min(1572864, +options.posterBudget || 0));
    let pixelBudget = Math.max(0, Math.min(1048576, +options.pixelBudget || 0));
    let captures = Math.max(0, Math.min(24, +options.pixelCount || 0));
    // Preserve the order in Google's viewer even when a related image was also
    // found ahead of it in the main grid. Poster updates follow the actual matches.
    const orderedRows = new Map([...panelRows].map(([key, entry]) => [key, entry.row]));
    for (const [key, row] of rows) if (!orderedRows.has(key)) orderedRows.set(key, row);
    for (const [key, row] of orderedRows) {
        const posterOnly = (state.scope === "related" || continued) && !branchMember(key);
        if (state.scope === "related" && !posterOnly) state.panelKeys.add(key);
        const size = Math.ceil(row.inline.length / 4) * 3;
        const stamp = size ? size + row.inline.slice(-24) : "";
        const sent = state.posters.get(key) === stamp || state.committedPosters.get(key) === stamp;
        const inline = size > 0 && size <= Math.min(65536, budget) && !sent ? row.inline : "";
        const newThumb = row.thumb && state.delivered.has(key) && !state.delivered.get(key);
        const image = imageForRow.get(row);
        let pixels = null;
        if (image && !state.rendered.has(key) && !state.committedRendered.has(key)) {
            if (captures && pixelBudget >= 4) pixels = renderedPixels(image, pixelBudget);
            // Another bounded snapshot can collect the remainder of an already-loaded grid.
            if (!pixels && (!captures || pixelBudget < 160 * 160 * 4) &&
                    captures < Math.min(24, options.pixelCount) && options.pixelBudget > pixelBudget) more = true;
        }
        if ((state.delivered.has(key) || posterOnly) && !inline && !newThumb && !pixels) continue;
        if (items.length === 160) { more = true; break; }
        if (inline) {
            budget -= size;
            state.posters.set(key, stamp);
        }
        if (pixels) {
            state.rendered.add(key);
            pixelBudget -= pixels[0] * pixels[1] * 4;
            captures--;
        }
        items.push({...row, inline, pixels, posterOnly});
        state.delivered.set(key, row.thumb);
    }
    state.inspectedOperation = options.operation;
    state.dirty = more;
    return {state: "ready", items, hasPanel: panelMounted, canExpand: [...state.roots.keys()].some(key => targets.has(key) && !state.visited.has(key))};
})
