// Loads Aladin Lite without letting its servers hold up the page.
//
// The usual CDS hosts (aladin.cds.unistra.fr, alasky) stopped answering from the
// observatory on 2026-11-05, and a blocking <script> pointing at them left the whole
// Planning page stuck loading. This loads the same library from jsDelivr, in the
// background, and takes the DSS images from ESA's mirror.
window.ALADIN_SURVEY = "https://skies.esac.esa.int/DSSColor/";
window.aladinReady =
  window.aladinReady ||
  import("https://cdn.jsdelivr.net/npm/aladin-lite@3.8.2/dist/aladin.js").then(function (module) {
    window.A = module.default;
    return window.A.init;
  });
