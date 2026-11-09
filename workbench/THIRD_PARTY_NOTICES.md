# Third-party references and assets

Dahe Chen · Workbench source handoff

- **Cloudscape Design System** components/global styles are installed through official npm packages. The frontend extracts the package's official font assets for same-origin hosting and preserves notices under `apps/web/public/fonts/THIRD-PARTY-LICENSES.txt` and built `dist/fonts/`. See the installed package notices and [official project](https://github.com/cloudscape-design/components).
- **React, Vite, TypeScript and their dependencies** remain subject to the licenses delivered in their npm packages. Exact resolved dependencies are recorded in `apps/web/package-lock.json`; no AWS product branding or private AWS code is included.
- **HolmesGPT** is an upstream Apache-2.0 project used as the intended integration foundation. This delivery contains our protocol parser, not a bundled/running Holmes engine. Protocol audit identity: `869054f0058604109325bdc9e9d0d3d27ff883bd`. See [source and license](https://github.com/HolmesGPT/holmesgpt/tree/869054f0058604109325bdc9e9d0d3d27ff883bd).
- **RCAEval** supplies the Metric BARO implementation. The live loader verifies the hashes of two source files from the user-installed [pinned upstream revision](https://github.com/phamquiluan/RCAEval/tree/dc9f48786db50cf9469f2c596b79f01907d4ac91) and executes the required function definitions. The source handoff includes a separate aggregate RE2 Online Boutique comparison receipt. The RCAEval checkout, datasets, and raw evaluation records are installed or obtained separately.
- **Prometheus 3.10.0** runs as a separately installed local process. [Official release](https://github.com/prometheus/prometheus/releases/tag/v3.10.0).
- **Python dependencies** are installed from PyPI under their respective licenses. Preserve their package notices when redistributing installed environments.

No license for the original AutoTriager application code is specified here. Third-party licenses apply to the corresponding dependencies.
