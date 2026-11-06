# Changelog

## [1.0.0](https://github.com/brewcoua/kvasir/compare/v0.4.0...v1.0.0) (2026-11-06)


### ⚠ BREAKING CHANGES

* KVASIR_SECRET_KEY is required, and a plain database file does not open yet.
* /v1/sessions and the costorm run kind no longer exist. A stored sessions.ttl_hours setting makes PATCH /v1/settings return 422 until it is deleted with DELETE /v1/settings/sessions.ttl_hours.
* **config:** kvasir.toml, KVASIR_CONFIG and every KVASIR_<SECTION>_* variable are gone. Store those settings in kvasir.db instead; see the README's Configuration section.
* **api:** the service does not start without KVASIR_TOKEN.
* OPENAI_API_KEY, OPENAI_API_BASE, LOG_LEVEL, LOG_FORMAT and the flat KVASIR_* names (KVASIR_SEARXNG_URL, KVASIR_EMBEDDING_MODEL, KVASIR_MODEL_WRITER, KVASIR_RUN_STORE, ...) are no longer read. Model names take a provider prefix in place of openai/. The model_writer request option is removed. /readyz returns {"providers": {...}} in place of "gateway".
* /v1/research, /v1/article and /v1/defaults are removed. /v1/research/clarify is now /v1/clarify. Research runs are recorded under the kind `ledger`.
* **research:** /v1/research drops max_rounds and queries_per_round for max_searches, max_attempts, follow_ups, max_depth, max_entities and max_criteria. Claims carry step instead of round. The Open WebUI valves MAX_ROUNDS and QUERIES_PER_ROUND become MAX_SEARCHES and FOLLOW_UPS.
* POST /v1/research no longer runs STORM. Clients use POST /v1/article with the same body.

### Features

* **api:** add cache stats, a whole-cache flush, and a site link graph ([a91ca25](https://github.com/brewcoua/kvasir/commit/a91ca25bf8f6e316ef3711f648894bf893b5862a))
* **api:** add routes to list, change, refresh and explain source lists ([507e5f4](https://github.com/brewcoua/kvasir/commit/507e5f4964b22092c05fcb3a61b58c60c634b6ee))
* **api:** answer a site's tags for many hosts at once ([de08507](https://github.com/brewcoua/kvasir/commit/de08507fa72935b627c2e93bfb315566f61e643a))
* **api:** cap run events per class and update counters in place ([968288c](https://github.com/brewcoua/kvasir/commit/968288c9813c7a2e910ff718d95c443a43f86b79))
* **api:** guard every route with a shared token ([f044c39](https://github.com/brewcoua/kvasir/commit/f044c39b52add56284b5053f77596db2f4ee8216))
* **api:** list the providers and the models each serves ([0e1923e](https://github.com/brewcoua/kvasir/commit/0e1923eaf3fd735ac8d3206a1afa5fb369026808))
* **api:** read and change the stored settings, applied live ([8c06777](https://github.com/brewcoua/kvasir/commit/8c06777d59bba65e9e989831b641cf92c426ee5a))
* **api:** serve site favicons from a local cache ([1bbaf6b](https://github.com/brewcoua/kvasir/commit/1bbaf6bba2057f9138a9bac9157acf1869c2ad71))
* choose research by method at one run route ([ee7dc96](https://github.com/brewcoua/kvasir/commit/ee7dc96c5536953d4d3fb1c4dac64085af54fa9b))
* **config:** store settings in kvasir.db, drop the TOML file ([0d89881](https://github.com/brewcoua/kvasir/commit/0d8988116f09293d5ab704b71225a3a9d564f4fb))
* dashboard timeline, live usage per stage, result drawer ([8edf733](https://github.com/brewcoua/kvasir/commit/8edf733b7142561e1e7c915c2c566b4dce8cf26d))
* **dashboard:** full-page results, source badges, themes ([a5425f2](https://github.com/brewcoua/kvasir/commit/a5425f21a1d6de2227c474f1c26d4f11059cd8a6))
* **dashboard:** launch runs, clarify first, and show each model call ([ed09f9b](https://github.com/brewcoua/kvasir/commit/ed09f9bd180ceec3b9747a1949b5d56602c319b3))
* **db:** encrypt plain database files when they open ([2b22642](https://github.com/brewcoua/kvasir/commit/2b226421ad334b3f239523d43b727349190140d9))
* deep research report pipeline at /v1/research, with clarify ([9894f99](https://github.com/brewcoua/kvasir/commit/9894f993c2c410e26111962bd6a2d2663a481b79))
* **dev:** run the whole system with just dev, seeding a first start ([a9a9e9d](https://github.com/brewcoua/kvasir/commit/a9a9e9dbff8ba106430c2ea818e34732d1eafb86))
* encrypt every database under a required KVASIR_SECRET_KEY ([45e33d3](https://github.com/brewcoua/kvasir/commit/45e33d3edd18bfd511d3a233d62c71d65bbccebe))
* **eval:** research evaluation harness, and more search engines ([e4530e2](https://github.com/brewcoua/kvasir/commit/e4530e2679631478b06dcd9ee4a1328a0ecba748))
* **favicons:** prefer sanitized SVG icons, and look further for a site's icon ([3271620](https://github.com/brewcoua/kvasir/commit/32716203a456d02e223c281b1865d21f9fa8a7df))
* **favicons:** serve stored icons without waiting for a worker, and rest silent sites ([54cc1f1](https://github.com/brewcoua/kvasir/commit/54cc1f13c56f946d6cc88b1d5f027c37e98408f6))
* **graph:** research by time, proof-number picking, and readable reports ([c0c88d5](https://github.com/brewcoua/kvasir/commit/c0c88d5f9499b9b6f1f5730eb60dea512801ae95))
* **ledger:** order questions by worth and close spent branches ([d39a362](https://github.com/brewcoua/kvasir/commit/d39a362071908ce6a0f224df9389deedc1c968e5))
* **ledger:** verify ranking and table cells ([af9cc6b](https://github.com/brewcoua/kvasir/commit/af9cc6b107b70ab406e1c48a516d766a49657eb9))
* local start from a config file, with reasoning effort ([a579190](https://github.com/brewcoua/kvasir/commit/a579190a1efb2d4fb947c9c703a8f49154e9cabc))
* **methods:** add the graph method ([0e29e59](https://github.com/brewcoua/kvasir/commit/0e29e596795ad5ccc8c279c8e6a58921d57663ab))
* **models:** add a reason tier for the judgements ([17c7216](https://github.com/brewcoua/kvasir/commit/17c721687633b2e4ab9fa81ef4ff05af1f1b9cae))
* move STORM to /v1/article ([9525176](https://github.com/brewcoua/kvasir/commit/95251763b0f08fd07237842671ef221bf61dcab3))
* name model providers in a structured config ([d7abf98](https://github.com/brewcoua/kvasir/commit/d7abf982b6a6a9a01dcd20a20adc5c21b2a20f64))
* **owui:** research model with clarifying questions first ([473d493](https://github.com/brewcoua/kvasir/commit/473d49339631794dbb8f8a063fb4269d8f633977))
* remove Co-STORM ([b5237d0](https://github.com/brewcoua/kvasir/commit/b5237d0d5b900b732e7c60f33b2d7e599a1229cd))
* **research:** add a grounding checker, corroboration and contradiction ([66d0f4f](https://github.com/brewcoua/kvasir/commit/66d0f4fd1abad12fe54dd0b9c641793afc12259d))
* **research:** cache what runs read, recall it, follow leads, and render JS pages ([f5d8dc7](https://github.com/brewcoua/kvasir/commit/f5d8dc72af21eeada240fe55338dd942034cbded))
* **research:** classify sites by kind, tags and trust tier ([9c39e3f](https://github.com/brewcoua/kvasir/commit/9c39e3ff5b4d2e19dff2f7aa054affde3d9953d0))
* **research:** drop quotes a negation turns around, and flag snippet claims ([959f659](https://github.com/brewcoua/kvasir/commit/959f659897bbb4429652d5d3e7bcb34deba86852))
* **research:** name each question with a short title ([bdba497](https://github.com/brewcoua/kvasir/commit/bdba4979cceda7fcc149e976363131ecd4bad405))
* **research:** pick search results by fused ranking before reading ([1278b44](https://github.com/brewcoua/kvasir/commit/1278b446b21e2706510248dc9de1c42eb3fb43a8))
* **research:** place progress events, and survive unreadable answers ([a6f3ff7](https://github.com/brewcoua/kvasir/commit/a6f3ff7ad75d27dbbd8105b217b7d05630b7c832))
* **research:** rank by source trust and never read avoid sites ([0caf24a](https://github.com/brewcoua/kvasir/commit/0caf24a20fd0e329ca10dc7f588036ffde04f356))
* **research:** rank or compare options on criteria ([8e284a1](https://github.com/brewcoua/kvasir/commit/8e284a12cdb7b891e30f3fa22f9dbb3483e78576))
* **research:** read PDF sources ([2d1502e](https://github.com/brewcoua/kvasir/commit/2d1502e5f3ba2f655f53d44b24d9b0a8ef794941))
* **research:** search again for sentences verification removed ([8c3914a](https://github.com/brewcoua/kvasir/commit/8c3914a0852a57a373d32f25e716dbc0f1670840))
* **research:** search question by question instead of in rounds ([ff201b0](https://github.com/brewcoua/kvasir/commit/ff201b02249b308c9abd45dde7afbee35efd3ec4))
* **research:** tag likely content farms from sitemaps and Tranco rank ([ec9d9ea](https://github.com/brewcoua/kvasir/commit/ec9d9ea9bcd8ee790ec0930c5bc8a803c11c3912))
* **research:** tag sites from Wikipedia's perennial sources list ([65f41af](https://github.com/brewcoua/kvasir/commit/65f41af304df7c155917044ebd34e978cb716fbd))
* **research:** verify cited sentences against their quotes ([411514e](https://github.com/brewcoua/kvasir/commit/411514ee95742e50078df4a61b43133644e8eb00))
* **research:** verify cited sentences through an escalation cascade ([81a4efd](https://github.com/brewcoua/kvasir/commit/81a4efde5c9a2112a4e0db5d69eeaf4ff5393547))
* **runs:** let a run turn source lists on and off for itself ([487a46c](https://github.com/brewcoua/kvasir/commit/487a46c65aabb6666f1607dc74f8b2fce1d8a152))
* **runs:** optional SQLite run history, with retention ([43059c7](https://github.com/brewcoua/kvasir/commit/43059c71621707d74020cafc851d0b53111746ee))
* **runs:** place progress events in a step tree ([8a45e8b](https://github.com/brewcoua/kvasir/commit/8a45e8b67bf63480b7f1c31590f3090e8ef1ca7b))
* **runs:** record searches and show the graph of a graph run ([e26fee1](https://github.com/brewcoua/kvasir/commit/e26fee145d0ee00c5d3f7ebe595de951212f6dc7))
* **runs:** say which sites a run left out and the lists behind it ([04c06af](https://github.com/brewcoua/kvasir/commit/04c06af8b806e31e171ca107e369dc1305c72d5c))
* **rust:** add kvasir eval ([bfb604a](https://github.com/brewcoua/kvasir/commit/bfb604ab9abbb9be021651a078d2e55abd3eaded))
* **rust:** add kvasir-api and the kvasir binary ([1b0047d](https://github.com/brewcoua/kvasir/commit/1b0047d37d2d8ef678ed4500a6a1cc9e806660e1))
* **rust:** add kvasir-browser ([538fb96](https://github.com/brewcoua/kvasir/commit/538fb96952462725c632a6930469c8cb47aa8273))
* **rust:** add kvasir-config ([56c870b](https://github.com/brewcoua/kvasir/commit/56c870b26cc496e7631975a9632070488690486b))
* **rust:** add kvasir-extract ([f7ca892](https://github.com/brewcoua/kvasir/commit/f7ca892bf99a2e5a5705dee5a2ce7c4cf76a1096))
* **rust:** add kvasir-fetch ([522a28e](https://github.com/brewcoua/kvasir/commit/522a28ecc96dd8ed9bb019b9ca10c3504f1d8cf5))
* **rust:** add kvasir-graph ([6195b61](https://github.com/brewcoua/kvasir/commit/6195b616f93c68ba91d760da09e23a4a61456769))
* **rust:** add kvasir-ledger ([f95f6bd](https://github.com/brewcoua/kvasir/commit/f95f6bd68fed731565a2d7458fc2dd3b147b99b7))
* **rust:** add kvasir-research ([b694113](https://github.com/brewcoua/kvasir/commit/b6941132f0557db40d65a443a3985e9096fa2eef))
* **rust:** add kvasir-sites ([62772a8](https://github.com/brewcoua/kvasir/commit/62772a870449a30c111b0b04ac29e7b2e2274992))
* **rust:** add kvasir-storm ([a482d66](https://github.com/brewcoua/kvasir/commit/a482d6638469d8bdfe4b038f837cec70fe975641))
* **rust:** add kvasir-types and the schema of every database ([3e39cac](https://github.com/brewcoua/kvasir/commit/3e39cac43895d5c016590d77bf248747db8a5770))
* **rust:** add the API schemas and kvasir-run ([3eec581](https://github.com/brewcoua/kvasir/commit/3eec58143bc18c6adcffff1d5ccc88e7ae5b1e05))
* **rust:** add the gateway client, roles, predict and kvasir-testkit ([b784ff5](https://github.com/brewcoua/kvasir/commit/b784ff5e5c6bb4c552f5fb99625d0902e6752e2d))
* **rust:** render and parse signatures as dspy does ([44fb5d2](https://github.com/brewcoua/kvasir/commit/44fb5d2766acc67f57f4d8f2717259c127491cb5))
* **search:** add arXiv as a search provider ([7026385](https://github.com/brewcoua/kvasir/commit/70263853dce4d65c74d596ea3ae8485ad8adeb0a))
* **search:** add Bing as a scraped search provider ([1db87e1](https://github.com/brewcoua/kvasir/commit/1db87e14d971a579faf642aec98a7e5ff55e6ace))
* **search:** add Brave as a search provider ([e4739dc](https://github.com/brewcoua/kvasir/commit/e4739dc85cda0b259ede4cfb36ae693fc9c94db1))
* **search:** add Crossref as a search provider ([d2c72c1](https://github.com/brewcoua/kvasir/commit/d2c72c18e08a60282d03e33735a3eda547c31802))
* **search:** add DuckDuckGo as a scraped search provider ([f851f63](https://github.com/brewcoua/kvasir/commit/f851f6359f85f024c8bd257db80110a23ada5ad1))
* **search:** add Europe PMC as a search provider ([8523676](https://github.com/brewcoua/kvasir/commit/85236764cf3b3cc234b1d3f205e55c349f51b827))
* **search:** add Exa as a search provider ([50b1607](https://github.com/brewcoua/kvasir/commit/50b1607fbd2c86fa7dd4136ecd9cf99c6cd36457))
* **search:** add Mojeek as a search provider ([d375620](https://github.com/brewcoua/kvasir/commit/d3756205380dc1699a256dfb1cc9c91539ccbd5e))
* **search:** add Mojeek's pages as a scraped search provider ([2b8a045](https://github.com/brewcoua/kvasir/commit/2b8a045b21cc566c64cb683fbec3a945b0b7dfe6))
* **search:** add OpenAlex as a search provider ([f1b1745](https://github.com/brewcoua/kvasir/commit/f1b1745f784349fc1134b703489a9282f280568e))
* **search:** add Semantic Scholar as a search provider ([3b68452](https://github.com/brewcoua/kvasir/commit/3b68452f21d9c434c93c39d81ce6256b005e2987))
* **search:** add Startpage as a scraped search provider ([ba1f806](https://github.com/brewcoua/kvasir/commit/ba1f806dadc6d3f06c431d8bdb082bcf8adbf570))
* **search:** add Tavily as a search provider ([669ded4](https://github.com/brewcoua/kvasir/commit/669ded409592026379a533ea0bb1cbdf59db00ed))
* **search:** add Wikipedia as a search provider ([e932851](https://github.com/brewcoua/kvasir/commit/e9328511a66d6e2436f83f9256a824a0cf34c477))
* **search:** let a person clear a provider's block from the page ([f710d26](https://github.com/brewcoua/kvasir/commit/f710d26eead2ebe591808950796af2ab942d7881))
* **search:** query providers side by side and fuse their ranks ([67a59b8](https://github.com/brewcoua/kvasir/commit/67a59b87374848f9a79d737b120e397fbcabea6a))
* **search:** require any web provider, and show each one's state ([93ac0f7](https://github.com/brewcoua/kvasir/commit/93ac0f786bdb8783f815c887c92b857a54279547))
* **settings:** add a secret field kind and nested setting keys ([4974ffa](https://github.com/brewcoua/kvasir/commit/4974ffa49f85a0604b69423f2cbd81127df60456))
* show each source's kind and trust, and let sites be marked ([4e0eea9](https://github.com/brewcoua/kvasir/commit/4e0eea9879df800a2142b989f3e91539a0db062c))
* **sources:** apply each list's effect, with exempt, except and tags, in the resolution order ([91e8d81](https://github.com/brewcoua/kvasir/commit/91e8d81239d389c87004ee4a40beb6b2c14db71e))
* **sources:** index each list as sorted hosts, and let every list naming a host count ([1d88050](https://github.com/brewcoua/kvasir/commit/1d880505361f4d45e62091d333fe8b294bdb90ca))
* **sources:** keep a catalog of host lists and an operator's own in sources.db ([6d75675](https://github.com/brewcoua/kvasir/commit/6d75675aae5554877f937af5c062d5fc9c73360a))
* **sources:** ship the .gov list in the catalog, on, instead of as a config default ([153178c](https://github.com/brewcoua/kvasir/commit/153178c82e5bb427efa80aa0514af8c14372a51c))
* **sources:** spare popular and suffix-rule sites from guarded lists, by sources.guard_rank ([7d5ad9c](https://github.com/brewcoua/kvasir/commit/7d5ad9c0884cd02ae0017abf06f980f9bc1636aa))
* **web:** add a Knowledge page, run options for the cache, and skipped stages ([76f8440](https://github.com/brewcoua/kvasir/commit/76f84407b3acc52863442f80c40286cc5ea9a77b))
* **web:** add a page to choose, edit, add and test source lists ([c3a77f6](https://github.com/brewcoua/kvasir/commit/c3a77f62e6f44a193a5662f4ac9348904d56875b))
* **web:** compact step cards, and progress with a percentage and ETA ([80fbf4a](https://github.com/brewcoua/kvasir/commit/80fbf4a6891eb4701bcd06edd81c4b373b38a035))
* **web:** draw a bypass around skipped stages in the step chart ([40fa036](https://github.com/brewcoua/kvasir/commit/40fa03686ccefc9408cf73fff3523c55f82e02ac))
* **web:** draw the graph of a graph run as a force layout ([5807b32](https://github.com/brewcoua/kvasir/commit/5807b322dac71e1126e9fa8ee2d39c36599a03b3))
* **web:** edit lists by dragging, and spell acronyms in setting labels ([64f1a47](https://github.com/brewcoua/kvasir/commit/64f1a47fa7043f9b50b90472ca0b3b388f6348e3))
* **web:** first-start setup page and service settings ([9804134](https://github.com/brewcoua/kvasir/commit/9804134450787641f8a9eb76fb3045b4a8e77fb8))
* **web:** fold progress events into their steps, and steady the tables ([80ec0d3](https://github.com/brewcoua/kvasir/commit/80ec0d3717a14284719bcd2816be4e23fe0fe288))
* **web:** group the search providers in settings and setup ([5bc3cab](https://github.com/brewcoua/kvasir/commit/5bc3cabcf8ddec1f8bab866cbca1b22f4ef4f486))
* **web:** link a run's steps across stages, with subflows by default ([fbc6bae](https://github.com/brewcoua/kvasir/commit/fbc6bae0c84675794303ad892aee0acb502b61d1))
* **web:** make cards with no content one header high, and wrap columns by weight ([44ceb2f](https://github.com/brewcoua/kvasir/commit/44ceb2f608bc76945d8cad697c5c35b72ea27565))
* **web:** rebuild the runs page as a Svelte app with its own pages ([a5983a9](https://github.com/brewcoua/kvasir/commit/a5983a989e239f57ce093f51f9791339dcc6e1e9))
* **web:** rework settings into grouped tabs with search, one place per setting ([3d25c73](https://github.com/brewcoua/kvasir/commit/3d25c73a39e14728a93f40b40b973bd6c3871a58))
* **web:** show a run's steps as an expandable tech tree ([661fab9](https://github.com/brewcoua/kvasir/commit/661fab9d80fb1c1cf731bde2565397ebe4511fd8))
* **web:** show attempts and recalls as rows, linked by rails, with repair under verify ([b539d6f](https://github.com/brewcoua/kvasir/commit/b539d6f098e8ce4cd079d6e8f0ad0af6220f6a07))
* **web:** show only the number on citation badges ([5082dab](https://github.com/brewcoua/kvasir/commit/5082dab3ac0ff6b55214be14259be6869b4814fb))
* **web:** show site favicons on the graph and citations ([36ddcb9](https://github.com/brewcoua/kvasir/commit/36ddcb9a37eba9e4a8ade637b46b2679d8ca97d5))
* **web:** show the Knowledge page as a site graph with storage and flush ([3f2b050](https://github.com/brewcoua/kvasir/commit/3f2b0505e64bc6b0cb4602fd99038a8f205edfa1))
* **web:** show what verification rewrote, removed and could not judge ([cb86e63](https://github.com/brewcoua/kvasir/commit/cb86e63143c722ea7896dcc03ea372245e535a80))
* **web:** shrink split and search nodes, animate the link into a running question ([01e47fc](https://github.com/brewcoua/kvasir/commit/01e47fc919a26c144aa247815e4058bc1b651e7f))
* **web:** size knowledge sites by connected sites, and light up the hovered neighbourhood ([5b12998](https://github.com/brewcoua/kvasir/commit/5b1299835d01ba79bf2132a435a7be99446a0e61))


### Bug Fixes

* **api:** log each request, as uvicorn's access log did ([20dc20a](https://github.com/brewcoua/kvasir/commit/20dc20ae794e218271173e96ceb5acc5f799fa9c))
* **api:** refuse cookie writes from another origin, keep the token out of repr ([1dc8d0e](https://github.com/brewcoua/kvasir/commit/1dc8d0e2529926279819f97fefdcdb968fff39ff))
* **config:** describe rendering as the Rust build does it ([799b7d9](https://github.com/brewcoua/kvasir/commit/799b7d987db9989f30478b405a908bbc08b9c44d))
* **eval:** default to the port the service listens on ([a8fc7df](https://github.com/brewcoua/kvasir/commit/a8fc7df242f7a28f870ab2531cde16ca64576132))
* **favicons:** stop trying www on private suffixes, and quiet refused certificates ([3c22d0e](https://github.com/brewcoua/kvasir/commit/3c22d0e9f9336e34e121ddaad5a30f91685d0301))
* **fetch:** render a blocked page after releasing its fetch permit ([d2cc837](https://github.com/brewcoua/kvasir/commit/d2cc8377c96e36db3082f562ad7c33f52ff9d1e8))
* **image:** give Chromium a writable home in the browser target ([4471bc1](https://github.com/brewcoua/kvasir/commit/4471bc153d4579e36185eb83ed08cf584b88982d))
* keep a run's slot until its work ends ([303974e](https://github.com/brewcoua/kvasir/commit/303974e0b995e5d77989defb88b790d3fef8b7f9))
* **mock:** keep a looping replay in progress through its pause ([b7c4d6f](https://github.com/brewcoua/kvasir/commit/b7c4d6fc53de9ba7f4d28ab3cc4c40677c353187))
* read KVASIR_LOG_LEVEL as Python's logging names ([41d2575](https://github.com/brewcoua/kvasir/commit/41d257557cda8f3bd7ab36a93ae2f5c73d6e4f70))
* **research:** close gaps the review of the cascade found ([e33c652](https://github.com/brewcoua/kvasir/commit/e33c652f227331c0b183fd1eed474b37fcbc1333))
* **research:** count a country's site as government ([2cb8a4b](https://github.com/brewcoua/kvasir/commit/2cb8a4b816a635330f0324bd997d1e8521f4ef8c))
* **research:** do not warn about a source list not downloaded yet ([d32f165](https://github.com/brewcoua/kvasir/commit/d32f165801e6b20d4e95c8c777acfa9e3eee866a))
* **research:** give the brief room for a thinking strong model ([4a8bc1f](https://github.com/brewcoua/kvasir/commit/4a8bc1fa27d039f671264a01ba998f3f2c2e479c))
* **research:** ignore the perennial sources status of Wikipedia ([13169e0](https://github.com/brewcoua/kvasir/commit/13169e03d745252b1575a0b32045a4fce4311481))
* **research:** keep a table row's header with its quote ([0d72f66](https://github.com/brewcoua/kvasir/commit/0d72f66bf058893c1284129114001cdd08e00e0e))
* **research:** keep quoted claims, search for current sources ([b15069e](https://github.com/brewcoua/kvasir/commit/b15069e057af9462e66f80df3aa92f486fb26495))
* **research:** stop a run when every search fails ([3360778](https://github.com/brewcoua/kvasir/commit/3360778737c96d4a2dd1d9f1e7af4f1bb0d1286c))
* **research:** stop copied pages corroborating each other ([ab0db0d](https://github.com/brewcoua/kvasir/commit/ab0db0ddeaa54fa9346a7f382fcad4788bdbddb3))
* **rust:** make a run's future Send and give followers the status of each event ([38f2dc6](https://github.com/brewcoua/kvasir/commit/38f2dc61fa8099d206e92c11b019712f21ee78fa))
* **rust:** read lists on any line break and once at startup ([59fd340](https://github.com/brewcoua/kvasir/commit/59fd34006d8ac316ec61c26ff717e80890700a68))
* **search:** count the cookies table in the schema test, and stop a layout test racing the zoom ([0e03185](https://github.com/brewcoua/kvasir/commit/0e03185f714bee52cd958bb33195ba7de6ddf086))
* **search:** keep words whole when stripping a snippet's markup ([9894c4d](https://github.com/brewcoua/kvasir/commit/9894c4d26964312cb96309fe2a19e413f720d62a))
* **storm:** check a related page's URL at every hop ([80c2420](https://github.com/brewcoua/kvasir/commit/80c24209e04cbc2f7501ff6d1dbe99504c4f64ce))
* **storm:** follow searxng's redirect, survive an empty table ([317d8a4](https://github.com/brewcoua/kvasir/commit/317d8a4ed59c6f8ad75275b859cc8a2f75395b05))
* **web:** hide the connection handles on step cards ([d6e1ca1](https://github.com/brewcoua/kvasir/commit/d6e1ca12a5399fbe0ebe81c384440e6fde21dacd))
* **web:** pick a model from a real list in every browser ([b34fd37](https://github.com/brewcoua/kvasir/commit/b34fd37ce31ef243caca146a64f4c5832192e632))
* **web:** proxy WebSockets in dev, and follow KVASIR_PORT ([ebcef83](https://github.com/brewcoua/kvasir/commit/ebcef830b2c8e32077c8831a74d035b2be76345e))
* **web:** show a step card's connection dot only where a line ends on it ([b039a03](https://github.com/brewcoua/kvasir/commit/b039a03f9a12b34a2a46f70b1ce8a1f286e29f7a))

## [0.4.0](https://github.com/brewcoua/kvasir/compare/v0.3.0...v0.4.0) (2026-08-17)


### Features

* answer owui task calls without a run ([bea8292](https://github.com/brewcoua/kvasir/commit/bea8292c13d44219546da152f045da5277d129a0))

## [0.3.0](https://github.com/brewcoua/kvasir/compare/v0.2.0...v0.3.0) (2026-08-17)


### Features

* add API schemas and STORM output parsing ([7b42deb](https://github.com/brewcoua/kvasir/commit/7b42deb66e6761b9081e96d6f32d9e744fe33fa8))
* add Co-STORM sessions and their routes ([843101e](https://github.com/brewcoua/kvasir/commit/843101e8abffbdf85a67ec5d3be55ef8f7608867))
* add configuration parsing ([da55339](https://github.com/brewcoua/kvasir/commit/da55339b2704ee5ca387efc67bb1acc3d63ed8e6))
* add progress events and SSE framing ([70444cc](https://github.com/brewcoua/kvasir/commit/70444cc6d819bb8f71a19a08c90132562cfce83a))
* add the Co-STORM Open WebUI pipe ([53adde5](https://github.com/brewcoua/kvasir/commit/53adde52fe399fb8d1679ccbaad273a3dee82963))
* add the container image ([1412b1f](https://github.com/brewcoua/kvasir/commit/1412b1f06a4172176ec5c631ca78f33103618ad9))
* add the research route and operational endpoints ([cd76838](https://github.com/brewcoua/kvasir/commit/cd768383c108ad1499095c4d865fd3f3b19b1a5a))
* add the STORM Open WebUI pipe ([9ed677e](https://github.com/brewcoua/kvasir/commit/9ed677eaa72ff580746b945ff95e038665ffeb8a))
* build STORM and Co-STORM runners from settings ([30167fe](https://github.com/brewcoua/kvasir/commit/30167feaa99da900da4a492e2dfc60b266979e90))
* live runs page ([761a7ae](https://github.com/brewcoua/kvasir/commit/761a7aef004345d83fe48dc9b1bc6d6b14c1de47))
* move the fork onto dspy 3.3.0 ([536aa24](https://github.com/brewcoua/kvasir/commit/536aa24da5d9a6c85e52b26816b7cbde97e29021))
* **owui:** merge the pipes, stream the report ([9aea4ad](https://github.com/brewcoua/kvasir/commit/9aea4ad424e3f7d99c8a57c4b31211717aefa589))
* run registry and job API ([59071eb](https://github.com/brewcoua/kvasir/commit/59071eb5e408860037fa2af4f02faf70c09777c7))
* **storm:** callbacks for article generation and polishing ([1134033](https://github.com/brewcoua/kvasir/commit/11340338f13e4462f2394eb410f7a70a0bd775fc))
* **storm:** embed through the gateway, drop the local model ([c42775b](https://github.com/brewcoua/kvasir/commit/c42775ba91b89ba76e7b3432dafebdd139c6246f))
* structured logging with run context ([3b888b7](https://github.com/brewcoua/kvasir/commit/3b888b76d30a02da68dee7bdcd04462bff6d07d7))


### Bug Fixes

* **ci:** tag released images with their version ([71f4727](https://github.com/brewcoua/kvasir/commit/71f4727118e591809ee12b583f97381e45d0fc77))
* make knowledge_storm importable and drop the CUDA runtime ([308a875](https://github.com/brewcoua/kvasir/commit/308a875a6ba86dd42157cdbfd38945fb79d0e5db))
* **storm:** bound the nested thread pools ([8d6ad07](https://github.com/brewcoua/kvasir/commit/8d6ad074b9998eca84018fa8ac5174e5bb2b5a95))
* **storm:** CoStormRunner.from_dict honours lm_config and rm ([aa42ce9](https://github.com/brewcoua/kvasir/commit/aa42ce99ad7e9eac6eeaf6ab4c2aa9e508d0a62c))
* **storm:** error handling and correctness ([ce0a980](https://github.com/brewcoua/kvasir/commit/ce0a9807e2be1470240c857eb7cdac1b802ac3c5))


### Documentation

* document the service and both Open WebUI functions ([475910c](https://github.com/brewcoua/kvasir/commit/475910c278e0f06606d1b257e3273e9133581e80))
* resolve the encoder gateway question, Co-STORM is in scope ([26557e3](https://github.com/brewcoua/kvasir/commit/26557e312d0647a0bf7410c605ec2de20d3d3951))
* rewrite for the fork ([5c55eb1](https://github.com/brewcoua/kvasir/commit/5c55eb18f32ae3bc238ec38ea6a5d4c644d5452f))

## [0.2.0](https://github.com/brewcoua/kvasir/compare/kvasir-v0.1.0...kvasir-v0.2.0) (2026-08-17)


### Features

* add API schemas and STORM output parsing ([7b42deb](https://github.com/brewcoua/kvasir/commit/7b42deb66e6761b9081e96d6f32d9e744fe33fa8))
* add Co-STORM sessions and their routes ([843101e](https://github.com/brewcoua/kvasir/commit/843101e8abffbdf85a67ec5d3be55ef8f7608867))
* add configuration parsing ([da55339](https://github.com/brewcoua/kvasir/commit/da55339b2704ee5ca387efc67bb1acc3d63ed8e6))
* add progress events and SSE framing ([70444cc](https://github.com/brewcoua/kvasir/commit/70444cc6d819bb8f71a19a08c90132562cfce83a))
* add the Co-STORM Open WebUI pipe ([53adde5](https://github.com/brewcoua/kvasir/commit/53adde52fe399fb8d1679ccbaad273a3dee82963))
* add the container image ([1412b1f](https://github.com/brewcoua/kvasir/commit/1412b1f06a4172176ec5c631ca78f33103618ad9))
* add the research route and operational endpoints ([cd76838](https://github.com/brewcoua/kvasir/commit/cd768383c108ad1499095c4d865fd3f3b19b1a5a))
* add the STORM Open WebUI pipe ([9ed677e](https://github.com/brewcoua/kvasir/commit/9ed677eaa72ff580746b945ff95e038665ffeb8a))
* build STORM and Co-STORM runners from settings ([30167fe](https://github.com/brewcoua/kvasir/commit/30167feaa99da900da4a492e2dfc60b266979e90))
* live runs page ([761a7ae](https://github.com/brewcoua/kvasir/commit/761a7aef004345d83fe48dc9b1bc6d6b14c1de47))
* move the fork onto dspy 3.3.0 ([536aa24](https://github.com/brewcoua/kvasir/commit/536aa24da5d9a6c85e52b26816b7cbde97e29021))
* **owui:** merge the pipes, stream the report ([9aea4ad](https://github.com/brewcoua/kvasir/commit/9aea4ad424e3f7d99c8a57c4b31211717aefa589))
* run registry and job API ([59071eb](https://github.com/brewcoua/kvasir/commit/59071eb5e408860037fa2af4f02faf70c09777c7))
* **storm:** callbacks for article generation and polishing ([1134033](https://github.com/brewcoua/kvasir/commit/11340338f13e4462f2394eb410f7a70a0bd775fc))
* **storm:** embed through the gateway, drop the local model ([c42775b](https://github.com/brewcoua/kvasir/commit/c42775ba91b89ba76e7b3432dafebdd139c6246f))
* structured logging with run context ([3b888b7](https://github.com/brewcoua/kvasir/commit/3b888b76d30a02da68dee7bdcd04462bff6d07d7))


### Bug Fixes

* make knowledge_storm importable and drop the CUDA runtime ([308a875](https://github.com/brewcoua/kvasir/commit/308a875a6ba86dd42157cdbfd38945fb79d0e5db))
* **storm:** bound the nested thread pools ([8d6ad07](https://github.com/brewcoua/kvasir/commit/8d6ad074b9998eca84018fa8ac5174e5bb2b5a95))
* **storm:** CoStormRunner.from_dict honours lm_config and rm ([aa42ce9](https://github.com/brewcoua/kvasir/commit/aa42ce99ad7e9eac6eeaf6ab4c2aa9e508d0a62c))
* **storm:** error handling and correctness ([ce0a980](https://github.com/brewcoua/kvasir/commit/ce0a9807e2be1470240c857eb7cdac1b802ac3c5))


### Documentation

* document the service and both Open WebUI functions ([475910c](https://github.com/brewcoua/kvasir/commit/475910c278e0f06606d1b257e3273e9133581e80))
* resolve the encoder gateway question, Co-STORM is in scope ([26557e3](https://github.com/brewcoua/kvasir/commit/26557e312d0647a0bf7410c605ec2de20d3d3951))
* rewrite for the fork ([5c55eb1](https://github.com/brewcoua/kvasir/commit/5c55eb18f32ae3bc238ec38ea6a5d4c644d5452f))
