# Changelog

All notable changes to EasyCat are recorded here. The project uses
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) structure; the
versioning contract that begins at 1.0.0 is documented in
[docs/public-api.md](docs/public-api.md#deprecation-removal-policy).

## Unreleased

### Added

- Release-readiness checks, including installed version metadata and strict
  release-tag validation.

### Fixed

- Plain-mode agent streaming no longer re-runs the sentence segmenter over the
  whole pending text on every delta when no sentence boundary is possible.

## 0.1.0 - Unreleased

### Added

- Initial pre-release implementation of the EasyCat voice-agent framework.
