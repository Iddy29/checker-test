"""Authorize.net, charging a dollar.

The card is put through the merchant's own gift form: the merchant
takes the money and asks the issuer, so the answer is the issuer's.
The check without the money is the other gate, authnet.py.

An address the merchant refuses is not the card's fault, so another
address is tried.  The addresses that answer are remembered and tried
first.
"""

import asyncio
import logging
import time

logger = logging.getLogger("authnet_azz")


async def authnet_azz_check(cc, mm, yy, cvv, proxy=None):
    """The card is charged a dollar, and the issuer is asked.

    The merchant takes the money and asks the issuer, so the answer is
    the issuer's.  An address the merchant refuses is not the card's
    fault, so another address is tried.
    """
    start = time.time()

    from gates.authnet import _membershipworks_check

    verdict, message = await _membershipworks_check(cc, mm, yy, cvv, proxy, start,
                                                    amount_cents=100)
    if verdict != "Retry":
        return message

    return ("Error - the merchant would not take a gift from any address [%ss]"
            % round(time.time() - start, 2))
