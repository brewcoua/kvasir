# Changelog

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
