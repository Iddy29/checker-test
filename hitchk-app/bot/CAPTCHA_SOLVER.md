# The captcha solver, and how it is fitted in

This is the prompt that describes the captcha solving in this codebase:
what the two captchas are, which one has to be answered, how the
answer is asked for, how it is handed over, and what is known to go
wrong.  It is written so that the work can be carried on by someone
else, or by a machine, without reading all of it again first.

---

## 1. The short version

A Stripe Checkout page holds **two different hCaptcha challenges**, and
only one of them matters:

| | the page's captcha | the intent's captcha |
|---|---|---|
| the key | `ec637546-e9b8-447a-ab81-b5fb6d228ab8` (Stripe's own) | `c7faac4c-1cd7-4b1b-b2d4-42ba98d09c7a` (the **merchant's** key) |
| the frame | `b.stripecdn.com/…/HCaptchaInvisible.html` | `b.stripe.com/…/HCaptcha.html` |
| where its request data lives | in the frame's own script | in the fragment of `js.stripe.com/v3/hcaptcha-inner-<id>.html` |
| who asks for it | Stripe's own script, by itself | **us** |
| what it asks for | nothing visible | a **box to click**, and then Verify |
| can it be answered here | yes, it answers itself | **yes — it is a box, and boxes can be clicked** |

The whole of the past work came from answering the **first** one and
handing it to the **second** one's address.  Stripe takes the answer,
and then says *"Captcha challenge failed. Try again with a different
payment method."*  The HTTP 200 from the answer address means the
answer was **received**, not that it was **right**.

So: **the captcha to answer is the intent's one, in the frame that
carries it, with the key and the request data from that frame's own
address.**

---

## 2. Where the pieces live

| what | where |
|---|---|
| the asking code | `hitchk-app/bot/gates/stripe_captcha.py` → `ASK_THE_CAPTCHA` |
| the watching code | `hitchk-app/bot/gates/stripe_captcha.py` → `WATCH_THE_CAPTCHA` |
| the finding of the intent's challenge | `hitchk-app/bot/gates/stripe_captcha.py` → `intent_challenge(page)` |
| the solving | `hitchk-app/bot/local_hcaptcha_solver.py` → `solve_in_the_frame(...)` |
| the box and the Verify button | `local_hcaptcha_solver.py` → `_click_the_box(page)`, `_click_the_verify(page)` |
| the whole checkout in a browser | `hitchk-app/bot/gates/stripe_native.py` → `native_check(...)` |
| the gate | `hitchk-app/bot/gates/stripe_co.py` → `stripe_co_check(...)` |

---

## 3. How the answer is asked for

The way Stripe's own script asks, and the way that works:

```js
// put the captcha up
const id = window.hcaptcha.render(container, {
    sitekey: sitekey,
    size: 'invisible',
    callback: keep,                    // where the answer arrives
    'error-callback': (e) => { ... }   // where the refusal arrives
});

// ask for the answer; the request data goes in its own place here
const p = window.hcaptcha.execute(id, {async: true, rqdata: rqdata});
p.then(keep).catch(onError);
```

**What is known to be wrong, and what is right:**

- The request data belongs at the **top level of the execute
  options**, as `rqdata`.  It does **not** go inside
  `enterprisePayload` for the plain `api.js` build.  Stripe's own
  script does it this way, and it works.
- `execute(sitekey, {...})` — giving the **key** where the widget id
  belongs — is refused with **`invalid-captcha-id`**.  The captcha has
  to be **put up first**, and then the **widget id** given to
  execute.
- `render(container, opts, {enterprisePayload: {rqdata, host}})` is a
  different call.  With it, hCaptcha says **`challenge-expired`** when
  the request data is old, and gives nothing when it is not.
- The answer arrives as `t.response` (and `t.ekey`).

---

## 4. The request data is fresh every time

**The request data is made new on every read.**  Read it, wait ten
seconds, hand the answer over, and Stripe says the challenge is gone.
So:

1. Read the request data **immediately before** asking.
2. Never keep it from earlier in the run.

---

## 5. The intent's challenge: where everything is

When the card is sent and Stripe wants a challenge, Stripe's own frame
appears and its **address carries the whole thing**:

```
js.stripe.com/v3/hcaptcha-inner-<id>.html
  #intentId=pi_3UW…
  &clientSecret=pi_3UW…_secret_…
  &sitekey=c7faac4c-1cd7-4b1b-b2d4-42ba98d09c7a
  &rqdata=<the request data>
  &verifyUrl=/v1/payment_intents/pi_3UW…/verify_challenge
```

So the frame tells us:

- **which key** to ask with,
- **which request data** to ask with,
- **where** to hand the answer (`verifyUrl`),
- **which intent** it is about and its secret.

The answer is handed over like this, and only these three are needed
besides the answer itself:

```
POST https://api.stripe.com{verifyUrl}
  client_secret = {clientSecret}
  key           = {the publishable key}
  captcha_vendor_name = hcaptcha
  challenge_response_token = {the answer}
```

Anything else is refused as an unknown parameter.  These are known to
be **refused**: `captcha_token`, `passive_captcha_token`,
`passive_captcha_ekey`, `challenge_response`, `captcha_response_token`.

---

## 6. The box, and the picture challenge

The intent's captcha puts up a **box**.  It is in a frame of
hCaptcha's own, and the frame says which kind it is:

- `…/hcaptcha.html#frame=checkbox` — **the box**.  Click it.
- `…/hcaptcha.html#frame=challenge` — the **picture** challenge.

The box is clicked the way a person clicks it, not by a blind
`.click()`:

```python
spot = await el.bounding_box()
x = spot["x"] + spot["width"] / 2
y = spot["y"] + spot["height"] / 2
await page.mouse.move(x - 70, y - 50, steps=14)   # the move in
await page.wait_for_timeout(140)
await page.mouse.move(x, y, steps=9)              # the move onto it
await page.wait_for_timeout(110)
await page.mouse.down()
await page.mouse.up()
```

Then the **Verify** button is pressed.  Without that press nothing comes
back.

### The picture challenge

Sometimes hCaptcha will not answer at all and asks for **pictures**
instead.  It cannot be answered without eyes.  The signs:

- the words in the frame are one of *"tap on"*, *"click on"*,
  *"select all"*, *"find things"*, *"images of"*, *"which of the"*,
  *"people that"*, *"vehicles that"*, *"food and"*, *"hot and cold"*
  and the like;
- or the frame simply says *"Please try again. ⚠️ Verify"* and never
  goes on to anything.

**It is the address, not the browser.**  The same code, the same card,
the same browser — one address gives the answer and the next asks for
pictures.  So the answer is to **turn the address over**: when the
captcha asks for pictures, the card is tried again on another address,
and the same address is never used twice running.

---

## 7. The browser has to look like the computer it says it is

The browser says it is a mac with a screen of a certain size.  The
numbers have to agree with each other, or the captcha asks for
pictures:

- the screen sizes are mac screens (`1728x1117`, `1512x982`, …);
- `devicePixelRatio` is **2** on a mac, not 1;
- the graphics card is named like a mac's
  (`ANGLE (Apple, ANGLE Metal Renderer: Apple M1, Unspecified Version)`),
  and **not** a Windows one (`Direct3D11`) — a mac never reports that;
- the clock is `America/Chicago`, the language `en-US`, and the
  browser is Chrome 136 on macOS.

The browser also has to be the **same browser** as the one the answer
is asked in, and it is started **once** and kept: starting Chrome
takes about twenty seconds, which is more than the whole of the rest
of the work.

---

## 8. The page has to take the card

Three things stop the page taking the card, and all three are fixed:

1. **The country list lies over the boxes below it.**  It is a real
   `<select>` (it is disabled, and its options are the whole list), and
   it sits on top of the city and the postcode, so the mouse cannot
   reach them.  The focus is put on each box **from the script** and
   the words are **typed**, which is what a person does and the page
   takes.
2. **The tax cannot be worked out without the address**, and without
   the tax the page will not take the card at all.  The page says
   *"Apply Tax"* when the address is short.  The state is wanted by
   some places, so it is set too.
3. **The page's own captcha** must be left alone.  It answers itself.

---

## 9. How long it takes, and how to read it

| | before | now |
|---|---|---|
| the page opening | 45 s and then a failure | **0.1 s** |
| a card, with the browser warm | 100 s+ | **13–20 s** |

**The page never finishes loading.**  Its own script keeps the load
going, so waiting for `domcontentloaded` waits forever and then
reports the page as not opening.  Wait for the first answer instead
(`wait_until="commit"`), and then give the page a moment.

**The answer on the wire is looked at first**, so a card that was taken
or refused is known at once and there is no waiting for it.

---

## 10. What is answered, and what is not

**Answered, and known to work:**

- the page's captcha — by Stripe's own script, by itself;
- the intent's captcha — by us, in the frame that carries it, with
  the key and the request data from that frame's address, and the box
  clicked and Verify pressed.

**Not answered:**

- the **picture** challenge.  It needs eyes.  The way round it is a
  different address, and after that a picture solver.

---

## 11. The shape of the work, so it can be carried on

1. Open the checkout page in a browser that has already been started.
2. Put the card in the page by hand: the country, the address, then
   the card number, the expiry and the code — all typed.
3. Make sure the tax is worked out.
4. Press pay.
5. Watch the wire.  When the answer of the intent arrives, take it.
6. If the captcha wants pictures, **turn the address over** and start
   again.
7. Read the intent back and say what it says: taken, refused, or
   still asking for the second factor.

The last step is the one that matters.  A card that is refused at the
captcha is **not** a refused card, and the two must never be said the
same way.
