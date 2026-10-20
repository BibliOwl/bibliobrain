# Third-party notices

Bibliobrain is licensed under the GNU Affero General Public License version 3 only.
Third-party components retain their own licenses and are not relicensed under the AGPL.

This document covers components stored in the repository and the principal runtime
components installed from `requirements.lock`. The lockfile is the authoritative inventory
of Python package names and versions for a release.

## Components stored in this repository

| Component | Location | License | Notice |
| --- | --- | --- | --- |
| Marked | `static/vendor/marked/` | MIT and notices reproduced by the upstream distribution | See `static/vendor/marked/LICENSE`. |
| KaTeX, including its fonts | `static/vendor/katex/` | MIT; some contributed files carry additional compatible notices | See `static/vendor/katex/LICENSE` and the notices retained in the source files. |
| DOMPurify 3.4.12 | `static/vendor/dompurify/` | Apache-2.0 or MPL-2.0; Bibliobrain relies on the Apache-2.0 option | See `static/vendor/dompurify/LICENSE`. |
| Inter Variable Font | `static/fonts/inter-variable.woff2` | SIL Open Font License 1.1 | See `THIRD_PARTY_LICENSES/Inter-OFL-1.1.txt`. |
| JetBrains Mono Variable Font | `static/fonts/jetbrains-mono-variable.woff2` | SIL Open Font License 1.1 | See `THIRD_PARTY_LICENSES/JetBrainsMono-OFL-1.1.txt`. |

## Principal Python runtime components

Python dependencies are downloaded from their publishers during installation; their code is
not copied into this source repository. A packaged distribution that embeds a Python
environment must preserve the license and notice files shipped in every wheel, normally under
`*.dist-info/licenses/`, in addition to this summary.

| Component | Locked version | License information | Source |
| --- | --- | --- | --- |
| certifi | 2026.7.22 | MPL-2.0 | <https://github.com/certifi/python-certifi> |
| ChromaDB | 1.5.7 | Apache-2.0 | <https://github.com/chroma-core/chroma> |
| FlatBuffers | 25.12.19 | Apache-2.0 | <https://github.com/google/flatbuffers> |
| langchain-community | 0.4.1 | MIT | <https://github.com/langchain-ai/langchain> |
| langchain-core | 1.3.3 | MIT | <https://github.com/langchain-ai/langchain> |
| langchain-text-splitters | 1.1.2 | MIT | <https://github.com/langchain-ai/langchain> |
| LangSmith SDK | 0.10.9 | MIT | <https://github.com/langchain-ai/langsmith-sdk> |
| NumPy | 2.4.4 | BSD-3-Clause with permissively licensed bundled components | <https://github.com/numpy/numpy> |
| orjson | 3.11.9 | MPL-2.0 and an Apache-2.0 or MIT licensing option, as stated by its package metadata | <https://github.com/ijl/orjson> |
| pypdfium2 | 5.12.1 | BSD-3-Clause, Apache-2.0, and bundled PDFium dependency licenses | <https://github.com/pypdfium2-team/pypdfium2> |
| PyTorch | 2.14.0 | BSD-3-Clause; binary distributions include additional notices | <https://github.com/pytorch/pytorch> |
| SentencePiece | 0.2.1 | Apache-2.0 | <https://github.com/google/sentencepiece> |
| tokenizers | 0.22.2 | Apache-2.0 | <https://github.com/huggingface/tokenizers> |
| tqdm | 4.70.0 | MIT and MPL-2.0, as stated by its package metadata | <https://github.com/tqdm/tqdm> |

The remaining runtime packages are listed with exact versions and integrity hashes in
`requirements.lock`. Their installed distributions contain their applicable license metadata
and license texts.

## Binary distribution requirements

When producing an installer, portable archive, executable, container, or other distribution
that embeds third-party binaries:

1. include this file and the `THIRD_PARTY_LICENSES/` directory;
2. retain all `LICENSE`, `COPYING`, and `NOTICE` files from embedded distributions;
3. retain the complete `*.dist-info/licenses/` directories from Python wheels;
4. retain PyTorch's `LICENSE` and `NOTICE` files;
5. retain the complete license tree shipped by `pypdfium2`, including its PDFium build
   licenses;
6. identify the corresponding source location for MPL-2.0 components and any modifications
   made to them;
7. perform a fresh inventory for the exact release environment instead of relying only on this
   document.

After installing the exact release environment, collect these files into a staging directory
with:

```powershell
python scripts/collect_third_party_licenses.py --output build/third-party-licenses
```

The generated `manifest.json` records every locked distribution, its installed version, its
declared license expression, and the license or notice files copied from it. The packaging
process must include the complete output directory.

Model weights downloaded from Ollama or another provider are not included in this inventory.
They must be reviewed under the license attached to each model before being redistributed or
bundled with Bibliobrain.
