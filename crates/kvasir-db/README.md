# kvasir-db

Opening kvasir's SQLite files, migrating them by `user_version`, and running blocking database
work off the async runtime.

Each file has one connection behind a mutex. The service's writes are small and rare, so
serialising them costs nothing, and WAL lets a reader in another process see a consistent file.

## Modules

Under `src/`.

| Module | Holds |
| --- | --- |
| `lib.rs` | `Db`: one connection behind a mutex, and `call`, which runs work off the async runtime. |
| `schema.rs` | The migrations of each file kvasir keeps. |
