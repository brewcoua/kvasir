"""Nothing to set up.

This file used to point HOME at a tempdir and set ENCODER_API_TYPE, because importing the fork
wrote to the filesystem and constructing an Encoder read that variable. Neither is true any more.
It stays so pytest keeps rootdir on the repository.
"""
