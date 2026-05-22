from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('messaging', '0012_messageencrypteduploadintent'),
    ]

    operations = [
        migrations.CreateModel(
            name='MessageReaction',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('user_id', models.PositiveBigIntegerField(db_index=True)),
                ('reaction', models.CharField(choices=[('thumbs_up', 'Thumbs up'), ('heart', 'Heart'), ('laugh', 'Laugh'), ('surprised', 'Surprised'), ('sad', 'Sad')], max_length=20)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('message', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='reactions', to='messaging.message')),
            ],
            options={
                'ordering': ['created_at', 'id'],
            },
        ),
        migrations.AddIndex(
            model_name='messagereaction',
            index=models.Index(fields=['message', 'reaction'], name='messaging_m_message_1501e6_idx'),
        ),
        migrations.AddIndex(
            model_name='messagereaction',
            index=models.Index(fields=['user_id', 'updated_at'], name='messaging_m_user_id_eb8c0e_idx'),
        ),
        migrations.AddConstraint(
            model_name='messagereaction',
            constraint=models.UniqueConstraint(fields=('message', 'user_id'), name='uq_message_reaction_user'),
        ),
    ]
