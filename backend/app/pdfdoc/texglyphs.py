"""What a glyph of a TeX font really is.

PDFs made by TeX draw formulas with the Computer Modern / AMS / Euler Type 1 fonts, whose characters are known by
glyph name (`pi`, `summationdisplay`, `asteriskmath`). Their ToUnicode maps are often wrong (MuPDF reads π as "⇡",
ε as '"', ∂ as "@", "ff" as "↵"), so the name is taken from the embedded font program instead: MuPDF's glyph id is
FreeType's glyph index, i.e. the glyph's place in the font's CharStrings, which `charstring_names` reads (the way
SymbolScraper / InftyReader rely on the PDF's own font data instead of OCR).

`GLYPHS` gives each name its Unicode text and its LaTeX (math mode). Names come from the TeX font encodings
OML (cmmi), OMS (cmsy), OMX (cmex), OT1 (cmr) and the AMS / Euler fonts; Unicode values follow pdfTeX's
glyphtounicode tables. Names outside this table fall back to their Unicode (pylatexenc's table gives the LaTeX)."""
from __future__ import annotations

import binascii
import re

# -- the font program ---------------------------------------------------------------------------------------
_R_EEXEC = 55665


def _decrypt(data: bytes, r: int = _R_EEXEC, skip: int = 4) -> bytes:
    out = bytearray()
    for c in data:
        out.append(c ^ (r >> 8))
        r = ((c + r) * 52845 + 22719) & 0xFFFF
    return bytes(out[skip:])


_CHARSTRING = re.compile(rb"/([^\s/\[\]{}()<>%]+)\s+(\d+)\s+(?:RD|-\|)\s")


def charstring_names(font_program: bytes) -> list[str] | None:
    """Glyph names of a Type 1 font program (pfa or pfb) in CharStrings order, as FreeType indexes them (.notdef
    first). None when the program is not Type 1."""
    i = font_program.find(b"eexec")
    if i < 0:
        return None
    body = font_program[i + 5:]
    if body[:6].startswith(b"\x80\x02"):          # pfb segment header after the cleartext part
        body = body[6:]
    body = body.lstrip(b"\r\n\t ")
    if re.fullmatch(rb"[0-9a-fA-F]{4}", body[:4] or b"-"):   # pfa: the encrypted part in hex
        hexpart = re.sub(rb"[^0-9a-fA-F]", b"", body.split(b"cleartomark")[0])
        body = binascii.unhexlify(hexpart[: len(hexpart) // 2 * 2])
    plain = _decrypt(body)
    j = plain.find(b"/CharStrings")
    if j < 0:
        return None
    names: list[str] = []
    k = plain.find(b"begin", j)
    k = j if k < 0 else k + 5
    while (m := _CHARSTRING.search(plain, k)):
        names.append(m.group(1).decode("latin-1"))
        k = m.end() + int(m.group(2))
    if ".notdef" in names and names[0] != ".notdef":   # FreeType moves .notdef to index 0
        z = names.index(".notdef")
        names[0], names[z] = names[z], names[0]
    return names or None


# -- the glyphs ------------------------------------------------------------------------------------------------
G: dict[str, tuple[str, str]] = {}
AMS: dict[str, tuple[str, str]] = {}      # msam / msbm names (some, like `triangle`, mean another glyph in cmsy)


def _add(table: str, into: dict | None = None) -> None:
    """Lines of `name unicode latex` (unicode as text; `_` for a space-free empty value)."""
    for line in table.strip().splitlines():
        name, uni, tex = line.split(None, 2)
        (G if into is None else into)[name] = ("" if uni == "_" else uni, tex)


_GREEK = ("alpha α beta β gamma γ delta δ zeta ζ eta η theta θ iota ι kappa κ lambda λ mu μ nu ν xi ξ pi π rho ρ "
          "sigma σ tau τ upsilon υ chi χ psi ψ omega ω").split()
for n, u in zip(_GREEK[::2], _GREEK[1::2]):
    G[n] = (u, "\\" + n)
for n, u in (("Gamma", "Γ"), ("Delta", "Δ"), ("Theta", "Θ"), ("Lambda", "Λ"), ("Xi", "Ξ"), ("Pi", "Π"),
             ("Sigma", "Σ"), ("Upsilon", "Υ"), ("Phi", "Φ"), ("Psi", "Ψ"), ("Omega", "Ω")):
    G[n] = (u, "\\" + n)

# OML (cmmi, cmmib): the variant Greek letters and the math italic symbols
_add(r"""
epsilon1 ϵ \epsilon
epsilon ε \varepsilon
theta1 ϑ \vartheta
pi1 ϖ \varpi
rho1 ϱ \varrho
sigma1 ς \varsigma
phi ϕ \phi
phi1 φ \varphi
harpoonleftup ↼ \leftharpoonup
harpoonleftdown ↽ \leftharpoondown
harpoonrightup ⇀ \rightharpoonup
harpoonrightdown ⇁ \rightharpoondown
arrowhookleft ↩ \hookleftarrow
arrowhookright ↪ \hookrightarrow
triangleright ▹ \triangleright
triangleleft ◃ \triangleleft
star ⋆ \star
partialdiff ∂ \partial
flat ♭ \flat
natural ♮ \natural
sharp ♯ \sharp
slurbelow ⌣ \smile
slurabove ⌢ \frown
lscript ℓ \ell
dotlessi ı \imath
dotlessj ȷ \jmath
weierstrass ℘ \wp
vector ⃗ \vec
tie ⁀ \frown
""")

# OMS (cmsy): operators, relations, arrows, delimiters
_add(r"""
minus − -
periodcentered · \cdot
multiply × \times
asteriskmath ∗ \ast
divide ÷ \div
diamondmath ⋄ \diamond
plusminus ± \pm
minusplus ∓ \mp
circleplus ⊕ \oplus
circleminus ⊖ \ominus
circlemultiply ⊗ \otimes
circledivide ⊘ \oslash
circledot ⊙ \odot
circlecopyrt ◯ \bigcirc
openbullet ∘ \circ
bullet ∙ \bullet
equivasymptotic ≍ \asymp
equivalence ≡ \equiv
reflexsubset ⊆ \subseteq
reflexsuperset ⊇ \supseteq
lessequal ≤ \leq
greaterequal ≥ \geq
precedesequal ⪯ \preceq
followsequal ⪰ \succeq
similar ∼ \sim
approxequal ≈ \approx
propersubset ⊂ \subset
propersuperset ⊃ \supset
lessmuch ≪ \ll
greatermuch ≫ \gg
precedes ≺ \prec
follows ≻ \succ
arrowleft ← \leftarrow
arrowright → \rightarrow
arrowup ↑ \uparrow
arrowdown ↓ \downarrow
arrowboth ↔ \leftrightarrow
arrownortheast ↗ \nearrow
arrowsoutheast ↘ \searrow
similarequal ≃ \simeq
arrowdblleft ⇐ \Leftarrow
arrowdblright ⇒ \Rightarrow
arrowdblup ⇑ \Uparrow
arrowdbldown ⇓ \Downarrow
arrowdblboth ⇔ \Leftrightarrow
arrownorthwest ↖ \nwarrow
arrowsouthwest ↙ \swarrow
proportional ∝ \propto
prime ′ \prime
infinity ∞ \infty
element ∈ \in
owner ∋ \ni
triangle △ \triangle
triangleinv ▽ \triangledown
negationslash ̸ \not
mapsto ↦ \mapsto
universal ∀ \forall
existential ∃ \exists
logicalnot ¬ \neg
emptyset ∅ \emptyset
Rfractur ℜ \Re
Ifractur ℑ \Im
latticetop ⊤ \top
perpendicular ⊥ \bot
aleph ℵ \aleph
union ∪ \cup
intersection ∩ \cap
unionmulti ⊎ \uplus
logicaland ∧ \wedge
logicalor ∨ \vee
turnstileleft ⊢ \vdash
turnstileright ⊣ \dashv
floorleft ⌊ \lfloor
floorright ⌋ \rfloor
ceilingleft ⌈ \lceil
ceilingright ⌉ \rceil
braceleft { \{
braceright } \}
angbracketleft ⟨ \langle
angbracketright ⟩ \rangle
bar | |
bardbl ‖ \|
arrowbothv ↕ \updownarrow
arrowdblbothv ⇕ \Updownarrow
backslash \ \backslash
wreathproduct ≀ \wr
radical √ \sqrt
coproduct ⨿ \amalg
nabla ∇ \nabla
integral ∫ \int
unionsq ⊔ \sqcup
intersectionsq ⊓ \sqcap
subsetsqequal ⊑ \sqsubseteq
supersetsqequal ⊒ \sqsupseteq
section § \S
dagger † \dagger
daggerdbl ‡ \ddagger
paragraph ¶ \P
club ♣ \clubsuit
diamond ♢ \diamondsuit
heart ♡ \heartsuit
spade ♠ \spadesuit
""")

# AMS symbols (msam, msbm) seen in texts: boxes, slanted inequalities, logic, arrows
_add(r"""
squaredot ⊡ \boxdot
squareplus ⊞ \boxplus
squaremultiply ⊠ \boxtimes
square □ \square
squaresolid ■ \blacksquare
squaresmallsolid ▪ \blacksquare
diamond ◇ \Diamond
diamondsolid ◆ \blacklozenge
lozenge ◊ \lozenge
lozengesolid ⧫ \blacklozenge
triangle △ \vartriangle
triangledown ▽ \triangledown
trianglesolid ▲ \blacktriangle
triangledownsld ▼ \blacktriangledown
lessequalslant ⩽ \leqslant
greaterequalslant ⩾ \geqslant
lessorsimilar ≲ \lesssim
greaterorsimilar ≳ \gtrsim
lessorapproxeql ⪅ \lessapprox
greaterorapproxeql ⪆ \gtrapprox
lessequalgreater ⋚ \lesseqgtr
greaterlessequal ⋛ \gtreqless
equalsdots ≑ \doteqdot
defines ≜ \triangleq
checkmark ✓ \checkmark
therefore ∴ \therefore
because ∵ \because
arrowsquiggleright ⇝ \rightsquigarrow
arrowrighttwohead ↠ \twoheadrightarrow
arrowlefttwohead ↞ \twoheadleftarrow
arrowdblright ⇒ \Rightarrow
notequal ≠ \neq
notless ≮ \nless
notgreater ≯ \ngtr
notlessequal ≰ \nleq
notgreaterequal ≱ \ngeq
notsubseteql ⊈ \nsubseteq
notsupersetequal ⊉ \nsupseteq
emptysetbold ∅ \varnothing
planckover2pi ℏ \hbar
planckover2pi1 ℏ \hslash
complement ∁ \complement
""", AMS)

# OMX (cmex): big operators; sized delimiters and their pieces map to the plain delimiter (merged by the layout)
_add(r"""
summationtext ∑ \sum
summationdisplay ∑ \sum
producttext ∏ \prod
productdisplay ∏ \prod
coproducttext ∐ \coprod
coproductdisplay ∐ \coprod
integraltext ∫ \int
integraldisplay ∫ \int
contintegraltext ∮ \oint
contintegraldisplay ∮ \oint
uniontext ⋃ \bigcup
uniondisplay ⋃ \bigcup
intersectiontext ⋂ \bigcap
intersectiondisplay ⋂ \bigcap
unionmultitext ⨄ \biguplus
unionmultidisplay ⨄ \biguplus
logicalandtext ⋀ \bigwedge
logicalanddisplay ⋀ \bigwedge
logicalortext ⋁ \bigvee
logicalordisplay ⋁ \bigvee
unionsqtext ⨆ \bigsqcup
unionsqdisplay ⨆ \bigsqcup
circledottext ⨀ \bigodot
circledotdisplay ⨀ \bigodot
circleplustext ⨁ \bigoplus
circleplusdisplay ⨁ \bigoplus
circlemultiplytext ⨂ \bigotimes
circlemultiplydisplay ⨂ \bigotimes
""")
for size in ("big", "Big", "bigg", "Bigg"):
    for stem, uni, tex in (("parenleft", "(", "("), ("parenright", ")", ")"), ("bracketleft", "[", "["),
                           ("bracketright", "]", "]"), ("floorleft", "⌊", "\\lfloor"), ("floorright", "⌋", "\\rfloor"),
                           ("ceilingleft", "⌈", "\\lceil"), ("ceilingright", "⌉", "\\rceil"), ("braceleft", "{", "\\{"),
                           ("braceright", "}", "\\}"), ("angbracketleft", "⟨", "\\langle"),
                           ("angbracketright", "⟩", "\\rangle"), ("slash", "/", "/"), ("backslash", "\\", "\\backslash"),
                           ("radical", "√", "\\sqrt")):
        G[stem + size] = (uni, tex)
for stem, uni, tex in (("parenleft", "(", "("), ("parenright", ")", ")"), ("bracketleft", "[", "["),
                       ("bracketright", "]", "]"), ("braceleft", "{", "\\{"), ("braceright", "}", "\\}")):
    for piece in ("tp", "bt", "mid", "ex"):
        G[stem + piece] = (uni, tex)
for name, uni, tex in (("vextendsingle", "|", "|"), ("vextenddouble", "‖", "\\|"), ("braceex", "", ""),
                       ("arrowvertex", "", ""), ("arrowdblvertex", "", ""), ("radicalbt", "√", "\\sqrt"),
                       ("radicalvertex", "", ""), ("radicaltp", "", ""), ("hatwide", "^", "\\widehat"),
                       ("hatwider", "^", "\\widehat"), ("hatwidest", "^", "\\widehat"), ("tildewide", "~", "\\widetilde"),
                       ("tildewider", "~", "\\widetilde"), ("tildewidest", "~", "\\widetilde")):
    G[name] = (uni, tex)

# OT1 (cmr, cmbx …) as used in formulas: digits, punctuation, accents (placed over the next symbol by the layout)
_add(r"""
zero 0 0
one 1 1
two 2 2
three 3 3
four 4 4
five 5 5
six 6 6
seven 7 7
eight 8 8
nine 9 9
equal = =
plus + +
less < <
greater > >
parenleft ( (
parenright ) )
bracketleft [ [
bracketright ] ]
slash / /
colon : :
semicolon ; ;
comma , ,
period . .
exclam ! !
question ? ?
hyphen - -
endash – -
emdash — -
quoteright ’ '
quoteleft ‘ `
quotedblright ” ''
quotedblleft “ ``
ff ff ff
fi fi fi
fl fl fl
ffi ffi ffi
ffl ffl ffl
germandbls ß ß
circumflex ^ \hat
tilde ~ \tilde
dieresis ¨ \ddot
macron ¯ \bar
acute ´ \acute
grave ` \grave
caron ˇ \check
breve ˘ \breve
dotaccent ˙ \dot
ring ˚ \mathring
""")

ACCENTS = {"\\hat", "\\tilde", "\\ddot", "\\bar", "\\acute", "\\grave", "\\check", "\\breve", "\\dot", "\\mathring",
           "\\vec", "\\widehat", "\\widetilde"}
BIG_OPERATORS = {"\\sum", "\\prod", "\\coprod", "\\int", "\\oint", "\\bigcup", "\\bigcap", "\\biguplus", "\\bigwedge",
                 "\\bigvee", "\\bigsqcup", "\\bigodot", "\\bigoplus", "\\bigotimes"}
GLYPHS = G


def glyph(name: str, font: str) -> tuple[str, str] | None:
    """(Unicode, LaTeX) of a glyph name in a font."""
    if font[:4].upper() in ("MSAM", "MSBM") and name in AMS:
        return AMS[name]
    return G.get(name)

# font families whose glyphs are math; a family's letters may need a math alphabet
MATH_FONT = re.compile(r"^(CMMI|CMMIB|CMSY|CMBSY|CMEX|MSAM|MSBM|EUFM|EUFB|EUSM|EUSB|EURM|EURB|EUEX|RSFS|LMMathItalic|"
                       r"LMMathSymbols|LMMathExtension|LMMI|LMSY|LMEX|STIXMath|STIX.*Math|XITSMath|CambriaMath|Cambria-Math|"
                       r"LatinModernMath|TeXGyre\w*Math|AsanaMath|Symbol|MTExtra|MTSY|MTMI|MTEX|MathematicalPi|Euclid)",
                       re.I)
ALPHABET = [(re.compile(r"^MSBM", re.I), "\\mathbb"), (re.compile(r"^EUF", re.I), "\\mathfrak"),
            (re.compile(r"^(EUS|RSFS)", re.I), "\\mathscr"), (re.compile(r"^(CMSY|CMBSY|LMSY)", re.I), "\\mathcal")]


def alphabet_of(font: str) -> str | None:
    """The math alphabet a font's Latin letters are set in (blackboard, fraktur, script, calligraphic), if any."""
    return next((cmd for pat, cmd in ALPHABET if pat.match(font)), None)
