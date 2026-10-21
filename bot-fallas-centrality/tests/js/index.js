// Punto de entrada para `node --test tests/js` (el comando del README).
//
// Por qué existe: en Node >= 21 los argumentos de `node --test` son patrones
// glob, y un directorio NO se expande a sus archivos de test: Node lo carga
// como módulo y, sin un index.js, falla con "Cannot find module .../tests/js".
// Con este archivo el directorio resuelve aquí y se importan los dos tests.
// `node --test "tests/js/*.test.mjs"` y `node --test` sin argumentos siguen
// funcionando y no recogen este archivo (no calza con *.test.*).
"use strict";

(async () => {
  await import("./plantillas.test.mjs");
  await import("./grilla.test.mjs");
})().catch((e) => {
  console.error(e);
  process.exitCode = 1;
});
