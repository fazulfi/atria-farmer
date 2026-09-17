"""Atria Farmer — automated account registration toolkit.

This package registers accounts on the Atria platform
(``auth.atria-asi.ai`` / ``api.atria-asi.ai``) end to end:

    captcha -> Logto registration -> e-mail OTP -> API key

It is built around three external pieces of infrastructure that the operator
supplies through the environment (see ``.env.example``):

1. **A 2captcha account** for solving the Aliyun Captcha 2.0 challenge.
2. **A Cloudflare Email Worker** that receives the OTP e-mail and exposes it
   over HTTP (``worker/`` in this repository).
3. **An HTTP proxy** (optional but strongly recommended) to avoid the
   per-IP rate limit on OTP delivery.

No credentials are hard-coded anywhere in this package.
"""

__version__ = "1.0.0"
__all__ = ["__version__"]
