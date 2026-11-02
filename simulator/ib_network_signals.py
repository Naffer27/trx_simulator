from django.db.models.signals import post_save
from django.dispatch import receiver
from .models import Referral, ReferralAttribution
from .ib_network import attach_from_attribution

@receiver(post_save, sender=Referral, dispatch_uid='ib_network_referral_created')
def referral_created(sender, instance, created, raw=False, **kwargs):
    if created and not raw:
        attach_from_attribution(instance)

@receiver(post_save, sender=ReferralAttribution, dispatch_uid='ib_network_attribution_created')
def attribution_created(sender, instance, created, raw=False, **kwargs):
    if created and not raw:
        referral = Referral.objects.filter(user_id=instance.referred_user_id).first()
        if referral:
            attach_from_attribution(referral)
