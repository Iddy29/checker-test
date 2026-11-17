"""The words of the bank, and what each of them means.

A card is judged by the issuer of the card, and the issuer answers in
its own words.  Every one of those words is a different thing, and the
person is to be told which one it is and what it means: "there is no
money on the card" and "the reason was not given" are two different
answers, and they must never be mixed or hidden behind one plain
"declined".

So the whole vocabulary is written down here, in one place, and both
the api and the native path speak it: the same card gives the same
word whichever way it is asked.
"""

# The word of the bank, and the word of ours.
THE_BANK_WORDS = {
    "INSUFFICIENT_FUNDS": "INSUFFICIENT_FUNDS",
    "INVALID_CVC": "INVALID_CVC",
    "INVALID_SECURITY_CODE": "INVALID_CVC",
    "CVV_FAILURE": "INVALID_CVC",
    "INCORRECT_CVC": "INVALID_CVC",
    "CVC_CHECK_FAILED": "INVALID_CVC",
    "INVALID_NUMBER": "INVALID_CARD",
    "INVALID_CARD": "INVALID_CARD",
    "INCORRECT_NUMBER": "INVALID_CARD",
    "NO_SUCH_ISSUER": "INVALID_CARD",
    "INVALID_EXPIRY_DATE": "INVALID_EXPIRY_DATE",
    "INVALID_EXPIRY": "INVALID_EXPIRY_DATE",
    "EXPIRED_CARD": "EXPIRED_CARD",
    "CARD_EXPIRED": "EXPIRED_CARD",
    "BASE_EXPIRED": "EXPIRED_CARD",
    "CARD_DECLINED": "CARD_DECLINED",
    "DECLINED": "CARD_DECLINED",
    "DO_NOT_HONOR": "DO_NOT_HONOR",
    "GENERIC_ERROR": "GENERIC_ERROR",
    "GENERIC_DECLINE": "GENERIC_ERROR",
    "INCORRECT_ZIP": "INCORRECT_ZIP",
    "INCORRECT_POSTAL_CODE": "INCORRECT_ZIP",
    "PROCESSING_ERROR": "PROCESSING_ERROR",
    "PROCESSOR_ERROR": "PROCESSING_ERROR",
    "LOST_CARD": "LOST_CARD",
    "LOST": "LOST_CARD",
    "STOLEN_CARD": "STOLEN_CARD",
    "STOLEN": "STOLEN_CARD",
    "RESTRICTED_CARD": "RESTRICTED_CARD",
    "RESTRICTED": "RESTRICTED_CARD",
    "TRANSACTION_NOT_ALLOWED": "TRANSACTION_NOT_ALLOWED",
    "AUTHENTICATION_REQUIRED": "3DS_REQUIRED",
    "THREE_D_SECURE": "3DS_REQUIRED",
    "SCA_REQUIRED": "3DS_REQUIRED",
    "ISSUER_NOT_AVAILABLE": "ISSUER_NOT_AVAILABLE",
    "DUPLICATE_TRANSACTION": "DUPLICATE_TRANSACTION",
    "PICKUP_CARD": "PICKUP_CARD",
    "PICK_UP_CARD": "PICKUP_CARD",
    "FRAUD_SUSPECTED": "FRAUD_SUSPECTED",
}

# What each of our words means, in the words the person reads.
THE_WORD_MEANS = {
    "INSUFFICIENT_FUNDS": ("the customer's payment method or bank account "
                           "does not have enough available funds to "
                           "complete the transaction"),
    "INVALID_CVC": "the card security code (CVV/CVC) is incorrect",
    "INVALID_CARD": "the card number is invalid",
    "INVALID_EXPIRY_DATE": "the card expiration date is invalid",
    "EXPIRED_CARD": "the card has expired",
    "CARD_DECLINED": "the bank declined the transaction",
    "DO_NOT_HONOR": ("the bank refused the transaction without giving a "
                     "specific reason"),
    "GENERIC_ERROR": ("a general payment-processing error; the exact "
                       "reason is unknown"),
    "INCORRECT_ZIP": "the billing ZIP/postal code is incorrect",
    "PROCESSING_ERROR": "an error occurred while processing the payment",
    "LOST_CARD": "the card was reported as lost",
    "STOLEN_CARD": "the card was reported as stolen",
    "RESTRICTED_CARD": "the card is restricted from making the transaction",
    "TRANSACTION_NOT_ALLOWED": ("the transaction is not permitted by the "
                                "bank or the card issuer"),
    "3DS_REQUIRED": "additional cardholder authentication is required",
    "ISSUER_NOT_AVAILABLE": ("the card issuer or bank is temporarily "
                              "unavailable"),
    "DUPLICATE_TRANSACTION": "the transaction appears to be a duplicate",
    "PICKUP_CARD": "the issuer requests that the card be retained",
    "FRAUD_SUSPECTED": "the issuer suspects fraud",
}


def the_bank_word(text: str) -> str:
    """The word of the bank in the words of ours.

    The whole vocabulary is looked through, and the word that fits best
    is the one that is used: the longest one, for "invalid_cvc" is not
    to be read as "invalid".
    """
    up = str(text or "").upper()
    best = ""
    for said, ours in THE_BANK_WORDS.items():
        if said in up and len(said) > len(best):
            best = said
    return THE_BANK_WORDS.get(best, "") if best else ""


def the_word_said(norm: str, said: str) -> str:
    """The word of the bank, and what it means.

    The person is to be told the reason of the refusal and not only the
    name of it.  The words of the shop are kept beside it, for the shop
    may say more than the name.
    """
    meaning = THE_WORD_MEANS.get(str(norm or "").upper())
    said = str(said or "").strip()
    if not meaning:
        return said or str(norm or "")
    if said and said.upper() != str(norm or "").upper():
        return "%s: %s -- the shop said: %s" % (norm, meaning, said)
    return "%s: %s" % (norm, meaning)
