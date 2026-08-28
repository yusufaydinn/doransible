"""Domain servisleri.

Alt paketler birbirine karışmamalıdır (servis katmanı ayrımı):

- ``ansible``  : runner, event normalization, validation çalıştırıcıları
- ``ai``       : provider abstraction ve artifact üretimi
- ``projects`` : project/inventory path yönetimi ve playbook keşfi
- ``security`` : path doğrulama, secret redaction, risk sınıflandırma
"""
