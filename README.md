# Stella API — قائمة سيرفرات جاهزة (مجاني ودائم)

هذا المجلد يحتوي خادم الجمع (aggregator) الخاص بـ Stella VPN:

1. يقرأ قائمة القنوات من
   `https://raw.githubusercontent.com/klamusto/gcp/refs/heads/main/gcp.txt`
2. يفتح معاينة كل قناة على `t.me/s/<channel>` (3 صفحات) ويجمع منشورات آخر 4 ساعات.
3. ينزّل ملف `.dark` عبر مواقع التحميل العامة (بدون حساب أو كابتشا).
4. يفك تشفير الملف (AES-CFB + MessagePack) ويستخرج بيانات VLESS/Trojan/VMess.
5. يكتب `servers.json` — وهذا هو الـAPI الذي يقرأه التطبيق في ثانية.

## التشغيل المجاني الدائم: GitHub Actions

المستودع العام (public) في GitHub يعطي دقائق Actions غير محدودة، والجدولة
أقل شيء كل 5 دقائق. لا يحتاج بطاقة بنكية ولا سيرفر.

### الخطوات

1. أنشئ مستودعاً عاماً جديداً باسم `stella-api` في حسابك
   (`klamusto` إذا كان هذا حسابك).
2. ارفع محتويات هذا المجلد إلى جذر المستودع:
   - `fetch_servers.py`
   - `dark_decode.py`
   - `requirements.txt`
   - `servers.json`
   - `cache.json`
   - `.github/workflows/update-servers.yml`
3. من المستودع: **Settings → Actions → General → Workflow permissions**
   اختر **Read and write permissions** ثم احفظ.
4. من تبويب **Actions** فعّل الووركفلو `update-servers` (سيظهر بعد أول رفع)،
   ثم اضغط **Run workflow** مرة واحدة للتجربة.
5. بعد دقيقة تقريباً افتح:
   `https://raw.githubusercontent.com/klamusto/stella-api/main/servers.json`
   يجب أن ترى `count` أكبر من صفر.

بعد ذلك يعمل تلقائياً كل 5 دقائق بلا أي تدخل.

### رابط التطبيق

التطبيق يستخدم افتراضياً:

```
https://raw.githubusercontent.com/klamusto/stella-api/main/servers.json
```

إذا غيّرت اسم المستودع أو الحساب، غيّر `DEFAULT_URL` في
`app/src/main/java/com/stella/vpn/dark/ApiFetch.java` وأعد بناء APK.

ويمكن أيضاً تجاوز الرابط دون إعادة بناء عبر SharedPreferences:

```bash
adb shell run-as com.stella.vpn ...
# key: api_url  (في ملف stella_vless.xml)
```

## ملاحظات مهمة

- **التوقيت:** GitHub قد يؤخر التشغيل 5–15 دقيقة وقت الضغط، لكن الملف يظل
  يخدم آخر 4 ساعات من السيرفرات، فلا يتعطل التطبيق.
- **الحصص:** مواقع التحميل تعطي 10–15 ملفاً لكل IP يومياً. كل تشغيل على
  GitHub يحصل على IP جديد عادةً، ومع ذلك نحدّ التحميل (`MAX_DL_PER_RUN=12`)
  لتجنب الحظر. المنشورات التي تفشل بسبب الحصة تُعاد المحاولة في التشغيل التالي.
- **الخمول:** GitHub يوقف الجداول بعد 60 يوماً بلا نشاط في المستودع. هذا
  الووركفلو يرفع commit كلما تغيّر الملف، وهذا يُبقي الجدول فعالاً.
- **السقف:** مدة الوظيفة 12 دقيقة كحد أقصى، والوظيفة العادية تنتهي خلال دقيقة.
- **بديل زمني أدق:** إذا أردت كل 5 دقائق بالضبط وبلا تأخير، شغّل نفس السكربت
  على `systemd timer` في خادم Oracle Cloud Always Free (ARM) واعرض
  `servers.json` عبر Nginx. نفس الملفات تعمل كما هي.

## تشغيل محلي (اختبار)

```bash
pip install -r requirements.txt
python fetch_servers.py
python -m http.server 8765
```

ثم ضع في التطبيق `api_url = http://<IP-الجهاز>:8765/servers.json`.
